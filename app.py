#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Fixed Waveform Viewer
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

# Create waveform directory
WAVEFORM_DIR = Path("/tmp/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"Waveform directory: {WAVEFORM_DIR}")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Models
class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"
    generate_waveform: bool = False

# Load problems
PROBLEMS = []
with open("problems.json", "r") as f:
    PROBLEMS = json.load(f)
logger.info(f"Loaded {len(PROBLEMS)} problems")

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "4.0"}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    simplified = []
    for problem in PROBLEMS:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"]
        })
    return {"problems": simplified}

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str, download: bool = False):
    """Serve waveform with professional HTML viewer"""
    try:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        html_path = WAVEFORM_DIR / f"{waveform_id}.html"
        
        if download and vcd_path.exists():
            return FileResponse(
                vcd_path,
                media_type="application/octet-stream",
                filename=f"{waveform_id}.vcd"
            )
        
        # Return professional HTML viewer
        html_content = create_professional_viewer(waveform_id, vcd_path.exists())
        return HTMLResponse(content=html_content)
            
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """Execute Verilog code"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Run simulation
        result = run_simulation(
            request.code,
            problem["testbench"],
            request.generate_waveform,
            problem["title"]
        )
        
        # Prepare response
        response = {
            "success": result["success"],
            "problem": problem["title"],
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", ""),
        }
        
        if not result["success"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        # Add waveform info
        if "waveform_id" in result:
            waveform_id = result["waveform_id"]
            response["waveform_id"] = waveform_id
            response["waveform_url"] = f"/api/waveform/{waveform_id}"
            response["waveform_download_url"] = f"/api/waveform/{waveform_id}?download=true"
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def run_simulation(user_code: str, testbench: str, generate_waveform: bool, problem_title: str) -> dict:
    """Run Verilog simulation"""
    waveform_id = None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Prepare testbench with VCD dump
        if generate_waveform:
            waveform_id = str(uuid.uuid4())
            vcd_path = str(tmp_path / "waveform.vcd")
            
            # Add dump commands to testbench
            if "$dumpfile" not in testbench:
                testbench = testbench.replace(
                    "initial begin",
                    f"initial begin\n    $dumpfile(\"{vcd_path}\");\n    $dumpvars(0);"
                )
        
        # Combine source
        source = f"`timescale 1ns/1ps\n{user_code}\n{testbench}"
        source_file = tmp_path / "design.v"
        source_file.write_text(source)
        
        # Compile
        output_exec = tmp_path / "sim"
        compile_result = subprocess.run(
            ["iverilog", "-o", str(output_exec), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if compile_result.returncode != 0:
            return {
                "success": False,
                "error": "Compilation Failed",
                "details": compile_result.stderr[:500]
            }
        
        # Simulate
        sim_result = subprocess.run(
            ["vvp", str(output_exec)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = sim_result.stdout + sim_result.stderr
        
        if "PASS" in output:
            result = {"success": True, "output": output}
            
            # Save waveform if requested
            if generate_waveform and waveform_id:
                vcd_file = tmp_path / "waveform.vcd"
                if vcd_file.exists() and vcd_file.stat().st_size > 0:
                    # Save VCD
                    dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                    import shutil
                    shutil.copy2(vcd_file, dest_vcd)
                    
                    logger.info(f"Waveform saved: {waveform_id}")
                    result["waveform_id"] = waveform_id
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def parse_vcd_signals(vcd_path: Path) -> tuple:
    """Parse VCD file to extract signal information and waveform data"""
    signals = []
    waveform_data = {}
    timescale = "1ns"
    
    try:
        with open(vcd_path, 'r') as f:
            content = f.read()
        
        lines = content.split('\n')
        
        # Parse signals and timescale
        signal_map = {}
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('$timescale'):
                parts = line.split()
                if len(parts) > 1:
                    timescale = parts[1]
            elif line.startswith('$var'):
                parts = line.split()
                if len(parts) >= 5:
                    signal_type = parts[1]
                    width = parts[2]
                    signal_id = parts[3]
                    signal_name = parts[4]
                    
                    # Remove $end if present
                    if signal_name.endswith('$end'):
                        signal_name = signal_name[:-4].strip()
                    
                    signal_map[signal_id] = signal_name
                    signals.append({
                        'id': signal_id,
                        'name': signal_name,
                        'type': signal_type,
                        'width': width,
                        'color': f"#{hash(signal_name) % 0xffffff:06x}"
                    })
        
        # Parse actual waveform data
        current_time = 0
        signal_states = {signal_id: 'x' for signal_id in signal_map.keys()}
        timeline = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#'):
                # Time change
                try:
                    time_val = int(line[1:])
                    if time_val != current_time:
                        # Record state at previous time
                        timeline.append({
                            'time': current_time,
                            'states': signal_states.copy()
                        })
                        current_time = time_val
                except:
                    continue
            elif line[0] in ['0', '1', 'x', 'z'] and len(line) > 1:
                # Signal value change
                value = line[0]
                signal_id = line[1:]
                if signal_id in signal_states:
                    signal_states[signal_id] = value
            elif line[0] in ['b', 'B']:
                # Bus value change
                parts = line.split()
                if len(parts) >= 2:
                    value = parts[0][1:]  # Remove 'b' prefix
                    signal_id = parts[1]
                    if signal_id in signal_states:
                        signal_states[signal_id] = value
        
        # Add final state
        if timeline:
            timeline.append({
                'time': current_time,
                'states': signal_states.copy()
            })
        
        # Convert to waveform data format
        for signal_id, signal_name in signal_map.items():
            waveform = []
            for timepoint in timeline:
                waveform.append({
                    'time': timepoint['time'],
                    'value': timepoint['states'][signal_id]
                })
            waveform_data[signal_name] = waveform
        
        # Limit signals for performance
        if len(signals) > 10:
            signals = signals[:10]
        
    except Exception as e:
        logger.error(f"Failed to parse VCD: {e}")
        # Return default signals
        signals = [
            {'id': '!', 'name': 'clk', 'type': 'wire', 'width': '1', 'color': '#ff6b6b'},
            {'id': '"', 'name': 'a', 'type': 'wire', 'width': '1', 'color': '#4ecdc4'},
            {'id': '#', 'name': 'b', 'type': 'wire', 'width': '1', 'color': '#ffd166'},
            {'id': '$', 'name': 'out', 'type': 'wire', 'width': '1', 'color': '#06d6a0'},
        ]
    
    return signals, waveform_data, timescale

def create_professional_viewer(waveform_id: str, vcd_exists: bool) -> str:
    """Create professional HTML viewer with actual waveform display"""
    
    # Parse VCD file
    signals_data = []
    waveform_data = {}
    timescale = "1ns"
    vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
    
    if vcd_exists and vcd_path.exists():
        try:
            signals_data, waveform_data, timescale = parse_vcd_signals(vcd_path)
        except Exception as e:
            logger.error(f"Error parsing VCD: {e}")
            signals_data = []
    
    # Convert data to JSON for JavaScript
    signals_json = json.dumps(signals_data)
    waveform_json = json.dumps(waveform_data)
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Waveform Viewer: {waveform_id}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        .header {{
            background: white;
            border-radius: 12px;
            padding: 25px 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }}
        
        .header h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 5px;
            color: #333;
        }}
        
        .header .subtitle {{
            font-size: 14px;
            color: #666;
        }}
        
        .waveform-container {{
            display: grid;
            grid-template-columns: 250px 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .signal-list-panel {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            max-height: 600px;
            overflow-y: auto;
        }}
        
        .signal-list-panel h3 {{
            font-size: 16px;
            margin-bottom: 15px;
            color: #333;
            padding-bottom: 10px;
            border-bottom: 2px solid #f0f0f0;
            position: sticky;
            top: 0;
            background: white;
            z-index: 1;
        }}
        
        .signal-item {{
            display: flex;
            align-items: center;
            padding: 12px 15px;
            margin-bottom: 8px;
            background: #f8f9fa;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            border-left: 4px solid #667eea;
        }}
        
        .signal-item:hover {{
            background: #e9ecef;
            transform: translateX(5px);
        }}
        
        .signal-item.active {{
            background: #e3f2fd;
            border-left-color: #2196f3;
        }}
        
        .signal-color {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 12px;
            flex-shrink: 0;
        }}
        
        .signal-name {{
            flex: 1;
            font-weight: 500;
            font-size: 14px;
        }}
        
        .waveform-panel {{
            background: white;
            border-radius: 12px;
            padding: 25px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .waveform-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 25px;
            padding-bottom: 15px;
            border-bottom: 2px solid #f0f0f0;
        }}
        
        .waveform-header h2 {{
            font-size: 18px;
            font-weight: 600;
            color: #333;
        }}
        
        .controls {{
            display: flex;
            gap: 10px;
        }}
        
        .btn {{
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }}
        
        .btn-primary {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }}
        
        .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }}
        
        .btn-secondary {{
            background: #f8f9fa;
            color: #495057;
            border: 1px solid #dee2e6;
        }}
        
        .btn-secondary:hover {{
            background: #e9ecef;
        }}
        
        .waveform-display {{
            background: #1a1a1a;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 25px;
            overflow-x: auto;
            position: relative;
            min-height: 400px;
        }}
        
        #waveform-container {{
            position: relative;
            min-height: 400px;
            overflow-x: auto;
        }}
        
        .waveform-grid {{
            position: relative;
            padding-top: 40px;
            padding-left: 100px;
        }}
        
        .signal-row {{
            position: relative;
            height: 60px;
            margin-bottom: 10px;
            border-bottom: 1px solid #333;
        }}
        
        .signal-label {{
            position: absolute;
            left: -100px;
            top: 20px;
            width: 90px;
            text-align: right;
            color: white;
            font-family: monospace;
            font-weight: bold;
        }}
        
        .signal-waveform {{
            position: relative;
            height: 100%;
        }}
        
        .time-grid {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 40px;
            border-bottom: 2px solid #444;
        }}
        
        .time-marker {{
            position: absolute;
            top: 0;
            width: 1px;
            height: 10px;
            background: #666;
        }}
        
        .time-label {{
            position: absolute;
            top: 12px;
            color: #888;
            font-size: 12px;
            transform: translateX(-50%);
            white-space: nowrap;
        }}
        
        .waveform-line {{
            position: absolute;
            height: 3px;
            background: #ff6b6b;
            top: 50%;
            transform: translateY(-50%);
            transition: background-color 0.3s;
        }}
        
        .waveform-transition {{
            position: absolute;
            width: 1px;
            background: #888;
            top: 30%;
            bottom: 30%;
        }}
        
        .waveform-value {{
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            color: white;
            font-family: monospace;
            font-size: 12px;
            padding: 2px 4px;
            background: rgba(0,0,0,0.7);
            border-radius: 3px;
            z-index: 10;
            display: none;
        }}
        
        .waveform-line:hover + .waveform-value {{
            display: block;
        }}
        
        .time-scale {{
            display: flex;
            justify-content: space-between;
            padding: 10px 5px;
            font-size: 12px;
            color: #666;
            border-top: 1px solid #e9ecef;
            margin-top: 15px;
        }}
        
        .info-panel {{
            background: white;
            border-radius: 12px;
            padding: 25px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }}
        
        .info-item {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
        }}
        
        .info-label {{
            font-size: 14px;
            color: #666;
            margin-bottom: 8px;
            font-weight: 500;
        }}
        
        .info-value {{
            font-size: 18px;
            font-weight: 600;
            color: #333;
        }}
        
        .status-badge {{
            display: inline-block;
            padding: 6px 12px;
            background: #10b981;
            color: white;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
        }}
        
        .action-buttons {{
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
        }}
        
        .btn-download {{
            background: linear-gradient(135deg, #10b981, #0da271);
            color: white;
        }}
        
        .btn-download:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(16, 185, 129, 0.4);
        }}
        
        .legend {{
            display: flex;
            gap: 20px;
            margin-top: 25px;
            padding-top: 20px;
            border-top: 1px solid #e9ecef;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .legend-color {{
            width: 16px;
            height: 16px;
            border-radius: 4px;
        }}
        
        .legend-text {{
            font-size: 14px;
            color: #666;
        }}
        
        .cursor-line {{
            position: absolute;
            top: 0;
            bottom: 0;
            width: 1px;
            background: #00ff00;
            z-index: 100;
            pointer-events: none;
            display: none;
        }}
        
        .cursor-time {{
            position: absolute;
            top: -25px;
            background: #00ff00;
            color: black;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: bold;
            transform: translateX(-50%);
            pointer-events: none;
        }}
        
        .footer {{
            text-align: center;
            padding: 20px;
            color: white;
            font-size: 14px;
        }}
        
        .footer a {{
            color: white;
            text-decoration: none;
            font-weight: 600;
        }}
        
        .footer a:hover {{
            text-decoration: underline;
        }}
        
        @media (max-width: 992px) {{
            .waveform-container {{
                grid-template-columns: 1fr;
            }}
            
            .info-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
        
        @media (max-width: 768px) {{
            .info-grid {{
                grid-template-columns: 1fr;
            }}
            
            .action-buttons {{
                flex-direction: column;
            }}
            
            .btn {{
                width: 100%;
                justify-content: center;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1><i class="fas fa-wave-square"></i> Digital Waveform Viewer</h1>
            <div class="subtitle">Waveform ID: {waveform_id} • Timescale: {timescale} • {len(signals_data)} signals</div>
        </div>
        
        <!-- Main Waveform Area -->
        <div class="waveform-container">
            <!-- Signal List -->
            <div class="signal-list-panel">
                <h3><i class="fas fa-list"></i> Signals</h3>
                <div id="signal-list">
                    <!-- Signals will be populated by JavaScript -->
                </div>
            </div>
            
            <!-- Waveform Display -->
            <div class="waveform-panel">
                <div class="waveform-header">
                    <h2><i class="fas fa-chart-line"></i> Waveform Visualization</h2>
                    <div class="controls">
                        <button class="btn btn-secondary" onclick="zoomIn()">
                            <i class="fas fa-search-plus"></i> Zoom In
                        </button>
                        <button class="btn btn-secondary" onclick="zoomOut()">
                            <i class="fas fa-search-minus"></i> Zoom Out
                        </button>
                        <button class="btn btn-primary" onclick="refreshViewer()">
                            <i class="fas fa-sync-alt"></i> Refresh
                        </button>
                    </div>
                </div>
                
                <div class="waveform-display">
                    <div id="waveform-container">
                        <div class="cursor-line" id="cursor-line">
                            <div class="cursor-time" id="cursor-time"></div>
                        </div>
                        <div id="waveform-grid" class="waveform-grid"></div>
                    </div>
                </div>
                
                <div class="time-scale" id="time-scale">
                    <!-- Time markers will be populated by JavaScript -->
                </div>
            </div>
        </div>
        
        <!-- Information Panel -->
        <div class="info-panel">
            <div class="info-grid">
                <div class="info-item">
                    <div class="info-label">Waveform ID</div>
                    <div class="info-value"><code>{waveform_id}</code></div>
                </div>
                <div class="info-item">
                    <div class="info-label">Format</div>
                    <div class="info-value">VCD (Value Change Dump)</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Status</div>
                    <div class="info-value">
                        <span class="status-badge">
                            <i class="fas fa-check-circle"></i> Ready
                        </span>
                    </div>
                </div>
                <div class="info-item">
                    <div class="info-label">Timescale</div>
                    <div class="info-value">{timescale}</div>
                </div>
            </div>
            
            <div class="action-buttons">
                <a href="/api/waveform/{waveform_id}?download=true" class="btn btn-download">
                    <i class="fas fa-download"></i> Download VCD File
                </a>
                <button class="btn btn-secondary" onclick="copyWaveformId()">
                    <i class="fas fa-copy"></i> Copy Waveform ID
                </button>
                <button class="btn btn-secondary" onclick="takeScreenshot()">
                    <i class="fas fa-camera"></i> Take Screenshot
                </button>
            </div>
            
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-color" style="background: #ff6b6b;"></div>
                    <div class="legend-text">High (1)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #4ecdc4;"></div>
                    <div class="legend-text">Low (0)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #ffd166;"></div>
                    <div class="legend-text">Unknown (x)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #06d6a0;"></div>
                    <div class="legend-text">High-Z (z)</div>
                </div>
            </div>
        </div>
        
        <!-- Footer -->
        <div class="footer">
            <p>© 2024 VLSI Practice Platform • <a href="/">Back to Editor</a> • Click and drag to pan, scroll to zoom</p>
        </div>
    </div>
    
    <script>
        // Waveform data from backend
        const signalsData = {signals_json};
        const waveformData = {waveform_json};
        const timescale = "{timescale}";
        
        let zoomLevel = 1.0;
        let panOffset = 0;
        let selectedSignals = [];
        let maxTime = 100; // Default max time
        let timeScale = 10; // Pixels per time unit
        const colors = ['#ff6b6b', '#4ecdc4', '#ffd166', '#06d6a0', '#118ab2', '#ef476f', '#7209b7'];
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            renderSignalList();
            calculateMaxTime();
            renderWaveform();
            setupMouseInteractions();
        }});
        
        function calculateMaxTime() {{
            maxTime = 0;
            for (const signalName in waveformData) {{
                const waveform = waveformData[signalName];
                if (waveform.length > 0) {{
                    const lastTime = waveform[waveform.length - 1].time;
                    if (lastTime > maxTime) {{
                        maxTime = lastTime;
                    }}
                }}
            }}
            // Add some padding
            maxTime = Math.ceil(maxTime / 10) * 10 + 10;
        }}
        
        function renderSignalList() {{
            const container = document.getElementById('signal-list');
            container.innerHTML = '';
            
            signalsData.forEach((signal, index) => {{
                const color = colors[index % colors.length];
                
                const div = document.createElement('div');
                div.className = 'signal-item';
                div.innerHTML = `
                    <div class="signal-color" style="background: ${{color}};"></div>
                    <div class="signal-name">${{signal.name}}</div>
                `;
                
                div.addEventListener('click', () => toggleSignal(signal.name));
                container.appendChild(div);
                
                // Auto-select first few signals
                if (index < 6) {{
                    selectedSignals.push(signal.name);
                    div.classList.add('active');
                }}
            }});
        }}
        
        function toggleSignal(signalName) {{
            const index = selectedSignals.indexOf(signalName);
            const signalItems = document.querySelectorAll('.signal-item');
            
            if (index === -1) {{
                selectedSignals.push(signalName);
                signalItems.forEach(item => {{
                    if (item.querySelector('.signal-name').textContent === signalName) {{
                        item.classList.add('active');
                    }}
                }});
            }} else {{
                selectedSignals.splice(index, 1);
                signalItems.forEach(item => {{
                    if (item.querySelector('.signal-name').textContent === signalName) {{
                        item.classList.remove('active');
                    }}
                }});
            }}
            
            renderWaveform();
        }}
        
        function renderWaveform() {{
            const container = document.getElementById('waveform-grid');
            container.innerHTML = '';
            
            // Clear cursor
            document.getElementById('cursor-line').style.display = 'none';
            
            // Create time grid
            const timeGrid = document.createElement('div');
            timeGrid.className = 'time-grid';
            container.appendChild(timeGrid);
            
            // Add time markers
            const timeStep = Math.max(10, Math.ceil(maxTime / 20));
            for (let time = 0; time <= maxTime; time += timeStep) {{
                const x = panOffset + (time * timeScale * zoomLevel);
                
                const marker = document.createElement('div');
                marker.className = 'time-marker';
                marker.style.left = x + 'px';
                
                const label = document.createElement('div');
                label.className = 'time-label';
                label.textContent = time + ' ' + timescale;
                label.style.left = x + 'px';
                
                timeGrid.appendChild(marker);
                timeGrid.appendChild(label);
            }}
            
            if (selectedSignals.length === 0) {{
                // Show message if no signals selected
                const message = document.createElement('div');
                message.style.position = 'absolute';
                message.style.top = '50%';
                message.style.left = '50%';
                message.style.transform = 'translate(-50%, -50%)';
                message.style.color = '#666';
                message.style.fontSize = '16px';
                message.textContent = 'Select signals from the left panel to view waveforms';
                container.appendChild(message);
                return;
            }}
            
            // Render each selected signal
            selectedSignals.forEach((signalName, signalIndex) => {{
                const signal = signalsData.find(s => s.name === signalName);
                if (!signal || !waveformData[signalName]) return;
                
                // Create signal row
                const row = document.createElement('div');
                row.className = 'signal-row';
                row.id = 'signal-row-' + signalName;
                
                // Signal label
                const label = document.createElement('div');
                label.className = 'signal-label';
                label.textContent = signalName;
                label.style.color = colors[signalIndex % colors.length];
                row.appendChild(label);
                
                // Waveform container
                const waveformContainer = document.createElement('div');
                waveformContainer.className = 'signal-waveform';
                row.appendChild(waveformContainer);
                
                container.appendChild(row);
                
                // Draw waveform
                drawSignalWaveform(signalName, waveformContainer, signalIndex);
            }});
        }}
        
        function drawSignalWaveform(signalName, container, colorIndex) {{
            const waveform = waveformData[signalName];
            if (!waveform || waveform.length === 0) return;
            
            const color = colors[colorIndex % colors.length];
            
            // Sort waveform by time
            waveform.sort((a, b) => a.time - b.time);
            
            let lastX = null;
            let lastValue = null;
            
            for (let i = 0; i < waveform.length; i++) {{
                const point = waveform[i];
                const nextPoint = waveform[i + 1];
                const x = panOffset + (point.time * timeScale * zoomLevel);
                
                // Draw horizontal line for current value
                if (lastX !== null) {{
                    const line = document.createElement('div');
                    line.className = 'waveform-line';
                    line.style.left = lastX + 'px';
                    line.style.width = (x - lastX) + 'px';
                    line.style.top = lastValue === '1' ? '25%' : lastValue === '0' ? '75%' : '50%';
                    line.style.height = lastValue === '1' || lastValue === '0' ? '3px' : '1px';
                    
                    if (lastValue === '1') {{
                        line.style.background = '#ff6b6b';
                    }} else if (lastValue === '0') {{
                        line.style.background = '#4ecdc4';
                    }} else if (lastValue === 'z') {{
                        line.style.background = '#06d6a0';
                        line.style.borderTop = '1px dashed #06d6a0';
                        line.style.borderBottom = '1px dashed #06d6a0';
                        line.style.height = '5px';
                    }} else {{
                        line.style.background = '#ffd166';
                        line.style.borderTop = '1px dashed #ffd166';
                        line.style.borderBottom = '1px dashed #ffd166';
                        line.style.height = '5px';
                    }}
                    
                    // Add hover tooltip
                    const tooltip = document.createElement('div');
                    tooltip.className = 'waveform-value';
                    tooltip.textContent = lastValue;
                    tooltip.style.left = (lastX + (x - lastX) / 2) + 'px';
                    line.appendChild(tooltip);
                    
                    container.appendChild(line);
                }}
                
                // Draw vertical transition line if value changes
                if (nextPoint && point.value !== nextPoint.value) {{
                    const transition = document.createElement('div');
                    transition.className = 'waveform-transition';
                    transition.style.left = x + 'px';
                    transition.style.background = '#888';
                    container.appendChild(transition);
                }}
                
                lastX = x;
                lastValue = point.value;
            }}
            
            // Draw final segment to maxTime
            if (lastX !== null) {{
                const finalX = panOffset + (maxTime * timeScale * zoomLevel);
                const line = document.createElement('div');
                line.className = 'waveform-line';
                line.style.left = lastX + 'px';
                line.style.width = (finalX - lastX) + 'px';
                line.style.top = lastValue === '1' ? '25%' : lastValue === '0' ? '75%' : '50%';
                line.style.height = lastValue === '1' || lastValue === '0' ? '3px' : '1px';
                
                if (lastValue === '1') {{
                    line.style.background = '#ff6b6b';
                }} else if (lastValue === '0') {{
                    line.style.background = '#4ecdc4';
                }} else if (lastValue === 'z') {{
                    line.style.background = '#06d6a0';
                    line.style.borderTop = '1px dashed #06d6a0';
                    line.style.borderBottom = '1px dashed #06d6a0';
                    line.style.height = '5px';
                }} else {{
                    line.style.background = '#ffd166';
                    line.style.borderTop = '1px dashed #ffd166';
                    line.style.borderBottom = '1px dashed #ffd166';
                    line.style.height = '5px';
                }}
                
                container.appendChild(line);
            }}
        }}
        
        function setupMouseInteractions() {{
            const container = document.getElementById('waveform-container');
            let isDragging = false;
            let startX = 0;
            
            container.addEventListener('mousedown', (e) => {{
                isDragging = true;
                startX = e.clientX - panOffset;
                container.style.cursor = 'grabbing';
            }});
            
            container.addEventListener('mousemove', (e) => {{
                if (isDragging) {{
                    panOffset = e.clientX - startX;
                    renderWaveform();
                    
                    // Show cursor with time
                    const rect = container.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const cursorLine = document.getElementById('cursor-line');
                    const cursorTime = document.getElementById('cursor-time');
                    
                    const time = Math.round((x - panOffset) / (timeScale * zoomLevel));
                    if (time >= 0 && time <= maxTime) {{
                        cursorLine.style.left = x + 'px';
                        cursorLine.style.display = 'block';
                        cursorTime.textContent = time + ' ' + timescale;
                        cursorTime.style.left = x + 'px';
                    }} else {{
                        cursorLine.style.display = 'none';
                    }}
                }}
            }});
            
            container.addEventListener('mouseup', () => {{
                isDragging = false;
                container.style.cursor = 'default';
            }});
            
            container.addEventListener('wheel', (e) => {{
                e.preventDefault();
                const rect = container.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const mouseTime = (mouseX - panOffset) / (timeScale * zoomLevel);
                
                if (e.deltaY < 0) {{
                    zoomLevel = Math.min(zoomLevel * 1.2, 5.0);
                }} else {{
                    zoomLevel = Math.max(zoomLevel / 1.2, 0.5);
                }}
                
                // Keep mouse position fixed
                panOffset = mouseX - (mouseTime * timeScale * zoomLevel);
                
                renderWaveform();
            }});
            
            // Show cursor on hover
            container.addEventListener('mousemove', (e) => {{
                if (!isDragging) {{
                    const rect = container.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const cursorLine = document.getElementById('cursor-line');
                    const cursorTime = document.getElementById('cursor-time');
                    
                    const time = Math.round((x - panOffset) / (timeScale * zoomLevel));
                    if (time >= 0 && time <= maxTime) {{
                        cursorLine.style.left = x + 'px';
                        cursorLine.style.display = 'block';
                        cursorTime.textContent = time + ' ' + timescale;
                        cursorTime.style.left = x + 'px';
                    }} else {{
                        cursorLine.style.display = 'none';
                    }}
                }}
            }});
        }}
        
        function zoomIn() {{
            zoomLevel = Math.min(zoomLevel * 1.2, 5.0);
            renderWaveform();
        }}
        
        function zoomOut() {{
            zoomLevel = Math.max(zoomLevel / 1.2, 0.5);
            renderWaveform();
        }}
        
        function refreshViewer() {{
            window.location.reload();
        }}
        
        function copyWaveformId() {{
            navigator.clipboard.writeText('{waveform_id}')
                .then(() => alert('Waveform ID copied to clipboard!'))
                .catch(() => alert('Failed to copy'));
        }}
        
        function takeScreenshot() {{
            const container = document.getElementById('waveform-container');
            html2canvas(container, {{
                backgroundColor: '#1a1a1a',
                scale: 2
            }}).then(canvas => {{
                const link = document.createElement('a');
                link.download = 'waveform-{waveform_id}.png';
                link.href = canvas.toDataURL('image/png');
                link.click();
            }});
        }}
    </script>
    <script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
</body>
</html>'''
    
    return html

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "waveforms": len(list(WAVEFORM_DIR.glob("*.vcd"))),
        "problems": len(PROBLEMS)
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
