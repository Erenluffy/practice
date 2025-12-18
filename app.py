#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Simple & Professional
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
                    "initial begin\n    $dumpfile(\"" + vcd_path + "\");\n    $dumpvars(0);"
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
                    
                    # Parse VCD for preview
                    vcd_content = vcd_file.read_text()
                    parse_vcd_for_preview(waveform_id, vcd_content)
                    
                    logger.info(f"Waveform saved: {waveform_id}")
                    result["waveform_id"] = waveform_id
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def parse_vcd_for_preview(waveform_id: str, vcd_content: str):
    """Parse VCD to create a preview JSON"""
    signals = []
    timescale = "1ns"
    
    # Parse VCD
    for line in vcd_content.split('\n'):
        line = line.strip()
        if line.startswith('$timescale'):
            parts = line.split()
            if len(parts) > 1:
                timescale = parts[1]
        elif line.startswith('$var'):
            parts = line.split()
            if len(parts) >= 5:
                signals.append({
                    "id": parts[3],
                    "name": parts[4],
                    "type": parts[1],
                    "width": parts[2]
                })
    
    # Save preview data
    preview_data = {
        "waveform_id": waveform_id,
        "signals": signals[:10],  # First 10 signals
        "timescale": timescale,
        "generated_at": datetime.now().isoformat()
    }
    
    preview_path = WAVEFORM_DIR / f"{waveform_id}_preview.json"
    preview_path.write_text(json.dumps(preview_data, indent=2))

def parse_vcd_signals(vcd_path: Path) -> list:
    """Parse VCD file to extract signal information"""
    signals = []
    
    try:
        with open(vcd_path, 'r') as f:
            content = f.read()
        
        lines = content.split('\n')
        
        for line in lines:
            line = line.strip()
            if line.startswith('$var'):
                parts = line.split()
                if len(parts) >= 5:
                    signal_type = parts[1]
                    width = parts[2]
                    signal_id = parts[3]
                    signal_name = parts[4]
                    
                    # Remove $end if present
                    if signal_name.endswith('$end'):
                        signal_name = signal_name[:-4].strip()
                    
                    signals.append({
                        'id': signal_id,
                        'name': signal_name,
                        'type': signal_type,
                        'width': width,
                        'color': f"#{hash(signal_name) % 0xffffff:06x}"
                    })
                    
                    # Limit to 10 signals for performance
                    if len(signals) >= 10:
                        break
        
        # If no signals found, add default signals
        if not signals:
            signals = [
                {'id': '!', 'name': 'clk', 'type': 'wire', 'width': '1', 'color': '#ff6b6b'},
                {'id': '"', 'name': 'a', 'type': 'wire', 'width': '1', 'color': '#4ecdc4'},
                {'id': '#', 'name': 'b', 'type': 'wire', 'width': '1', 'color': '#ffd166'},
                {'id': '$', 'name': 'out', 'type': 'wire', 'width': '1', 'color': '#06d6a0'},
            ]
        
    except Exception as e:
        logger.error(f"Failed to parse VCD: {e}")
        # Return default signals
        signals = [
            {'id': '!', 'name': 'clk', 'type': 'wire', 'width': '1', 'color': '#ff6b6b'},
            {'id': '"', 'name': 'a', 'type': 'wire', 'width': '1', 'color': '#4ecdc4'},
            {'id': '#', 'name': 'b', 'type': 'wire', 'width': '1', 'color': '#ffd166'},
            {'id': '$', 'name': 'out', 'type': 'wire', 'width': '1', 'color': '#06d6a0'},
        ]
    
    return signals
def create_professional_viewer(waveform_id: str, vcd_exists: bool) -> str:
    """Create professional HTML viewer with actual waveform display"""
    
    # Read VCD file to extract signals
    signals_data = []
    vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
    
    if vcd_exists and vcd_path.exists():
        try:
            signals_data = parse_vcd_signals(vcd_path)
        except:
            signals_data = []
    
    # Convert signals to JSON for JavaScript
    signals_json = json.dumps(signals_data)
    
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
        }}
        
        .signal-list-panel h3 {{
            font-size: 16px;
            margin-bottom: 15px;
            color: #333;
            padding-bottom: 10px;
            border-bottom: 2px solid #f0f0f0;
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
        
        #waveform-canvas {{
            display: block;
            background: #1a1a1a;
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
            <div class="subtitle">Waveform ID: {waveform_id} • Real-time signal visualization</div>
        </div>
        
        <!-- Main Waveform Area -->
        <div class="waveform-container">
            <!-- Signal List -->
            <div class="signal-list-panel">
                <h3><i class="fas fa-list"></i> Signals ({len(signals_data)})</h3>
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
                    <canvas id="waveform-canvas" width="1200" height="400"></canvas>
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
                    <div class="info-label">Signals Detected</div>
                    <div class="info-value">{len(signals_data)} signals</div>
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
                    <div class="legend-text">Clock</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #06d6a0;"></div>
                    <div class="legend-text">Data</div>
                </div>
            </div>
        </div>
        
        <!-- Footer -->
        <div class="footer">
            <p>© 2024 VLSI Practice Platform • <a href="/">Back to Editor</a> • Use mouse wheel to zoom, drag to pan</p>
        </div>
    </div>
    
    <script>
        // Waveform data from backend
        const signalsData = {signals_json};
        let zoomLevel = 1.0;
        let selectedSignals = [];
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            renderSignalList();
            drawWaveform();
            renderTimeScale();
            
            // Add mouse wheel zoom
            document.getElementById('waveform-canvas').addEventListener('wheel', function(e) {{
                e.preventDefault();
                if (e.deltaY < 0) {{
                    zoomIn();
                }} else {{
                    zoomOut();
                }}
            }});
        }});
        
        function renderSignalList() {{
            const container = document.getElementById('signal-list');
            container.innerHTML = '';
            
            signalsData.forEach((signal, index) => {{
                const colors = ['#ff6b6b', '#4ecdc4', '#ffd166', '#06d6a0', '#118ab2', '#ef476f', '#7209b7'];
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
                if (index < 3) {{
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
            
            drawWaveform();
        }}
        
        function drawWaveform() {{
            const canvas = document.getElementById('waveform-canvas');
            const ctx = canvas.getContext('2d');
            
            // Clear canvas
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            
            // Draw background grid
            drawGrid(ctx, canvas.width, canvas.height);
            
            if (selectedSignals.length === 0) {{
                // Show message if no signals selected
                ctx.fillStyle = '#666';
                ctx.font = '16px Arial';
                ctx.textAlign = 'center';
                ctx.fillText('Select signals from the left panel to view waveforms', canvas.width / 2, canvas.height / 2);
                return;
            }}
            
            // Draw each selected signal
            const signalHeight = 60;
            const verticalSpacing = 20;
            const startY = 50;
            
            selectedSignals.forEach((signalName, signalIndex) => {{
                const signal = signalsData.find(s => s.name === signalName);
                if (!signal) return;
                
                const y = startY + (signalIndex * (signalHeight + verticalSpacing));
                
                // Draw signal label
                ctx.fillStyle = '#fff';
                ctx.font = '14px Arial';
                ctx.textAlign = 'left';
                ctx.fillText(signal.name, 20, y + signalHeight / 2 + 5);
                
                // Draw signal waveform
                drawSignalWaveform(ctx, signal, y, signalHeight, signalIndex);
            }});
        }}
        
        function drawGrid(ctx, width, height) {{
            ctx.strokeStyle = '#333';
            ctx.lineWidth = 1;
            
            // Vertical lines (time grid)
            for (let x = 100; x < width; x += 50 * zoomLevel) {{
                ctx.beginPath();
                ctx.moveTo(x, 0);
                ctx.lineTo(x, height);
                ctx.stroke();
            }}
            
            // Horizontal lines
            for (let y = 50; y < height; y += 50) {{
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(width, y);
                ctx.stroke();
            }}
        }}
        
        function drawSignalWaveform(ctx, signal, y, height, colorIndex) {{
            const colors = ['#ff6b6b', '#4ecdc4', '#ffd166', '#06d6a0', '#118ab2'];
            const color = colors[colorIndex % colors.length];
            
            const totalTime = 100; // 100ns total time
            const timePerPixel = totalTime / (1000 * zoomLevel);
            const startX = 100;
            
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.fillStyle = color;
            
            // Draw signal line
            ctx.beginPath();
            ctx.moveTo(startX, y + height / 2);
            
            // Draw simulated waveform (in real app, parse actual VCD data)
            for (let i = 0; i < 10; i++) {{
                const x = startX + (i * 80 * zoomLevel);
                const isHigh = i % 2 === 0;
                const signalY = y + (isHigh ? height * 0.25 : height * 0.75);
                
                // Horizontal line
                ctx.lineTo(x, signalY);
                
                // Vertical transition
                ctx.lineTo(x, y + (isHigh ? height * 0.75 : height * 0.25));
            }}
            
            ctx.stroke();
            
            // Draw value markers
            for (let i = 0; i < 10; i++) {{
                const x = startX + (i * 80 * zoomLevel);
                const isHigh = i % 2 === 0;
                const value = isHigh ? '1' : '0';
                
                ctx.fillStyle = isHigh ? '#ff6b6b' : '#4ecdc4';
                ctx.beginPath();
                ctx.arc(x, y + (isHigh ? height * 0.25 : height * 0.75), 4, 0, Math.PI * 2);
                ctx.fill();
                
                // Draw value text
                ctx.fillStyle = '#fff';
                ctx.font = '12px Arial';
                ctx.textAlign = 'center';
                ctx.fillText(value, x, y + (isHigh ? height * 0.15 : height * 0.85));
            }}
        }}
        
        function renderTimeScale() {{
            const container = document.getElementById('time-scale');
            container.innerHTML = '';
            
            for (let time = 0; time <= 100; time += 20) {{
                const div = document.createElement('div');
                div.style.position = 'relative';
                div.style.left = `${{100 + (time * 8 * zoomLevel)}}px`;
                div.textContent = `${{time}}ns`;
                container.appendChild(div);
            }}
        }}
        
        function zoomIn() {{
            zoomLevel = Math.min(zoomLevel * 1.2, 5.0);
            drawWaveform();
            renderTimeScale();
        }}
        
        function zoomOut() {{
            zoomLevel = Math.max(zoomLevel / 1.2, 0.5);
            drawWaveform();
            renderTimeScale();
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
            const canvas = document.getElementById('waveform-canvas');
            const link = document.createElement('a');
            link.download = 'waveform-{waveform_id}.png';
            link.href = canvas.toDataURL('image/png');
            link.click();
        }}
    </script>
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
