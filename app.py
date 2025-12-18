#!/usr/bin/env python3
"""
Backend API for VLSI Practice with Waveform Generation
Run on your VPS: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import shutil
import uuid
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from collections import defaultdict

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

# Create waveform directory
WAVEFORM_DIR = Path("/tmp/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

# Mount static files for waveform access
app.mount("/waveforms", StaticFiles(directory=WAVEFORM_DIR), name="waveforms")

# CORS - ALLOW YOUR STATIC SITE
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:5501",
        "http://127.0.0.1:5501",
        "https://your-static-site.com",
        "https://*.onrender.com",
        "http://localhost:8000",
        "*"
    ],
    allow_methods=["POST", "GET", "OPTIONS", "DELETE"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Models
class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"
    generate_waveform: bool = False

# Load problems with error handling
PROBLEMS = []

def load_problems():
    """Load problems from JSON file or use defaults"""
    global PROBLEMS
    try:
        # Try different possible locations
        possible_paths = [
            "problems.json",
            "./problems.json",
            "/app/problems.json",
            os.path.join(os.path.dirname(__file__), "problems.json")
        ]
        
        for path in possible_paths:
            logger.info(f"Trying to load problems from: {path}")
            if os.path.exists(path):
                with open(path, "r") as f:
                    PROBLEMS = json.load(f)
                    logger.info(f"Loaded {len(PROBLEMS)} problems from {path}")
                    return True
    except Exception as e:
        logger.error(f"Failed to load problems.json: {e}")
        PROBLEMS = []
    
    logger.error(f"No problems loaded!")
    return False

# Load problems at startup
load_problems()

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "2.0", "features": ["iverilog", "gtkwave", "vcd_generation"]}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    try:
        # Try to reload problems
        load_problems()
        
        # Return problems with simplified structure for frontend
        simplified_problems = []
        for problem in PROBLEMS:
            simplified = {
                "id": problem["id"],
                "title": problem["title"],
                "description": problem["description"],
                "difficulty": problem["difficulty"],
                "category": problem["category"],
                "template": problem["template"]
            }
            simplified_problems.append(simplified)
        
        return JSONResponse(content={"problems": simplified_problems})
    except Exception as e:
        logger.error(f"Error in get_problems: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to load problems", "details": str(e)}
        )

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str):
    """Serve waveform files - returns HTML viewer by default"""
    try:
        logger.info(f"Requesting waveform: {waveform_id}")
        
        # Clean up any invalid characters
        waveform_id = waveform_id.strip()
        if not waveform_id:
            raise HTTPException(status_code=400, detail="Invalid waveform ID")
        
        # Check for HTML viewer first
        html_path = WAVEFORM_DIR / f"{waveform_id}.html"
        svg_path = WAVEFORM_DIR / f"{waveform_id}.svg"
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        if html_path.exists():
            logger.info(f"Serving HTML viewer: {html_path}")
            return HTMLResponse(content=html_path.read_text())
        elif svg_path.exists():
            logger.info(f"Serving SVG: {svg_path}")
            return FileResponse(
                svg_path,
                media_type="image/svg+xml",
                filename=f"{waveform_id}.svg"
            )
        elif vcd_path.exists():
            logger.info(f"Serving VCD: {vcd_path}")
            return FileResponse(
                vcd_path,
                media_type="application/octet-stream",
                filename=f"{waveform_id}.vcd"
            )
        else:
            logger.warning(f"Waveform not found: {waveform_id}")
            raise HTTPException(status_code=404, detail=f"Waveform not found: {waveform_id}")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """
    Execute Verilog code and return results with optional waveform
    """
    try:
        # 1. Find the problem
        problem = None
        for p in PROBLEMS:
            if p["id"] == request.problem_id:
                problem = p
                break
        
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        logger.info(f"Running problem: {problem['id']} for user: {request.user_id}")
        
        # 2. Run simulation
        result = run_real_simulation(
            request.code,
            problem["testbench"],
            generate_waveform=request.generate_waveform
        )
        
        # 3. Prepare response
        response = {
            "success": result["success"],
            "problem": problem["title"],
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", ""),
        }
        
        # Add hint if failed
        if not result["success"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        # Add waveform info if generated
        if "waveform_id" in result:
            response["waveform_id"] = result["waveform_id"]
            response["waveform_url"] = f"/api/waveform/{result['waveform_id']}"
            response["waveform_vcd_url"] = f"/waveforms/{result['waveform_id']}.vcd"
            if result.get("waveform_html", False):
                response["waveform_html_url"] = f"/waveforms/{result['waveform_id']}.html"
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in run_code: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

def modify_testbench_for_waveform(testbench_code: str, vcd_file_path: str) -> str:
    """
    Intelligently add $dumpfile and $dumpvars to testbench
    Returns modified testbench code with waveform dumping
    """
    # Check if testbench already has $dumpfile
    if "$dumpfile" in testbench_code:
        return testbench_code
    
    # Split testbench into lines
    lines = testbench_code.split('\n')
    modified_lines = []
    
    # Find where to insert dump commands
    found_initial = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Look for 'initial begin' line
        if "initial begin" in stripped and not found_initial:
            modified_lines.append(line)
            # Insert dump commands after initial begin
            indent = len(line) - len(line.lstrip())
            indent_str = " " * indent
            modified_lines.append(f"{indent_str}    $dumpfile(\"{vcd_file_path}\");")
            modified_lines.append(f"{indent_str}    $dumpvars(0);")
            found_initial = True
        else:
            modified_lines.append(line)
    
    # If no initial block found, create one
    if not found_initial:
        # Find the module declaration end
        for i, line in enumerate(lines):
            if ");" in line and "module" in lines[max(0, i-2):i+1]:
                # Insert after module declaration
                indent = len(line) - len(line.lstrip())
                indent_str = " " * indent
                modified_lines = lines[:i+1] + [
                    f"{indent_str}initial begin",
                    f"{indent_str}    $dumpfile(\"{vcd_file_path}\");",
                    f"{indent_str}    $dumpvars(0);",
                    f"{indent_str}end"
                ] + lines[i+1:]
                break
    
    return '\n'.join(modified_lines)

def run_real_simulation(user_code: str, testbench_code: str, generate_waveform: bool = False) -> dict:
    """
    Takes user's Verilog module and the hidden testbench,
    compiles and simulates using Icarus Verilog, returns results and optional waveform.
    """
    waveform_id = None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        logger.info(f"Running simulation in temp dir: {tmpdir}")
        
        # 1. Check for module name in user code
        if "module top_module" not in user_code:
            return {
                "success": False,
                "error": "Compilation Error",
                "details": "Your code must define 'module top_module' exactly.",
                "type": "compilation"
            }
        
        # 2. Prepare testbench
        if generate_waveform:
            waveform_id = str(uuid.uuid4())
            vcd_file_path = str(tmpdir_path / "waveform.vcd")
            
            # Modify testbench to include dump commands
            modified_testbench = modify_testbench_for_waveform(testbench_code, vcd_file_path)
            combined_source = f"""
`timescale 1ns/1ps
{user_code}
{modified_testbench}
"""
        else:
            combined_source = f"""
`timescale 1ns/1ps
{user_code}
{testbench_code}
"""
        
        # 3. Write source file
        source_file = tmpdir_path / "design.v"
        source_file.write_text(combined_source)
        logger.debug(f"Source file written to: {source_file}")
        
        # 4. Compile with Icarus Verilog
        output_executable = tmpdir_path / "sim"
        compile_cmd = ["iverilog", "-o", str(output_executable), "-g2012", str(source_file)]
        
        try:
            logger.info(f"Compiling with: {' '.join(compile_cmd)}")
            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if compile_result.returncode != 0:
                logger.error(f"Compilation failed: {compile_result.stderr}")
                return {
                    "success": False,
                    "error": "Compilation Failed",
                    "details": compile_result.stderr[:500],
                    "type": "compilation"
                }
            
            # 5. Simulate with vvp
            logger.info(f"Simulating with vvp")
            sim_result = subprocess.run(
                ["vvp", str(output_executable)],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            output = sim_result.stdout + sim_result.stderr
            logger.debug(f"Simulation output: {output[:500]}...")
            
            # 6. Parse the simulation output
            if "PASS" in output:
                result = {"success": True, "output": output}
                
                # 7. Generate waveform if requested
                if generate_waveform and waveform_id:
                    waveform_file = tmpdir_path / "waveform.vcd"
                    if waveform_file.exists() and waveform_file.stat().st_size > 0:
                        # Copy VCD to persistent storage
                        dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                        try:
                            shutil.copy2(waveform_file, dest_vcd)
                            logger.info(f"Waveform saved: {dest_vcd} ({dest_vcd.stat().st_size} bytes)")
                            
                            # Generate HTML waveform viewer
                            try:
                                html_file = generate_html_waveform_viewer(waveform_file, waveform_id)
                                if html_file:
                                    result["waveform_html"] = True
                                    logger.info(f"HTML waveform viewer generated: {html_file}")
                            except Exception as e:
                                logger.warning(f"HTML viewer generation failed (VCD still available): {e}")
                                # Create a simple fallback HTML
                                create_simple_waveform_fallback(waveform_id)
                            
                            result["waveform_id"] = waveform_id
                        except Exception as e:
                            logger.error(f"Failed to process waveform: {e}")
                    else:
                        logger.warning("Waveform file not created or empty during simulation")
                
                return result
            else:
                # Extract error message
                error_lines = [line for line in output.split('\n') if "FAIL" in line or "Error" in line or "error" in line]
                error_msg = error_lines[0] if error_lines else "Simulation failed"
                return {
                    "success": False,
                    "error": "Test Failed",
                    "details": error_msg,
                    "output": output[:1000]
                }
                
        except subprocess.TimeoutExpired:
            logger.error("Simulation timeout")
            return {
                "success": False,
                "error": "Timeout",
                "details": "Simulation took too long (30s limit)."
            }
        except Exception as e:
            logger.error(f"Simulation error: {e}", exc_info=True)
            return {
                "success": False,
                "error": "Simulation Error",
                "details": str(e)
            }

# ==================== VCD PARSER & WAVEFORM VISUALIZER ====================

def parse_vcd_file(vcd_path: Path) -> Dict:
    """
    Parse VCD file and extract signal data in a structured format
    """
    signals = {}
    signal_id_to_name = {}
    current_time = 0
    timeline = []
    
    with open(vcd_path, 'r') as f:
        content = f.read()
    
    lines = content.split('\n')
    section = None
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
            
        # Parse variable definitions
        if line.startswith('$var'):
            # $var wire 1 ! a $end
            parts = line.split()
            if len(parts) >= 5:
                var_type = parts[1]
                width = parts[2]
                signal_id = parts[3]
                signal_name = parts[4]
                
                signal_id_to_name[signal_id] = signal_name
                signals[signal_name] = {
                    'id': signal_id,
                    'type': var_type,
                    'width': width,
                    'values': [],  # List of (time, value) tuples
                    'waveform': []  # For HTML display
                }
        
        # Parse time markers
        elif line.startswith('#'):
            try:
                current_time = int(line[1:])
                timeline.append(current_time)
            except:
                pass
        
        # Parse value changes
        elif line and line[0] in '01bxz':
            # Format: 1! or 0" or b10 addr etc
            value = line[0]
            signal_id = line[1:]
            
            if signal_id in signal_id_to_name:
                signal_name = signal_id_to_name[signal_id]
                if signal_name in signals:
                    signals[signal_name]['values'].append((current_time, value))
    
    # Sort timeline
    timeline = sorted(set(timeline))
    
    return {
        'signals': signals,
        'timeline': timeline,
        'total_time': max(timeline) if timeline else 0
    }

def generate_waveform_data(vcd_data: Dict) -> Dict:
    """
    Convert parsed VCD data into waveform display format
    """
    signals = vcd_data['signals']
    timeline = vcd_data['timeline']
    
    if not timeline:
        return {}
    
    # Create time slots
    time_slots = []
    for i in range(len(timeline)):
        if i < len(timeline) - 1:
            time_slots.append((timeline[i], timeline[i+1]))
        else:
            time_slots.append((timeline[i], timeline[i] + 10))  # Add padding
    
    # Process each signal
    waveform_data = {}
    for signal_name, signal_info in signals.items():
        values = signal_info['values']
        if not values:
            continue
        
        # Create waveform string
        waveform = []
        current_value = 'x'  # Unknown initial state
        
        for time_slot in time_slots:
            start_time, end_time = time_slot
            
            # Find value at this time
            slot_value = current_value
            for val_time, val in values:
                if val_time <= start_time:
                    slot_value = val
                else:
                    break
            
            waveform.append({
                'start': start_time,
                'end': end_time,
                'value': slot_value,
                'duration': end_time - start_time
            })
            current_value = slot_value
        
        waveform_data[signal_name] = {
            'name': signal_name,
            'waveform': waveform,
            'color': get_signal_color(signal_name)
        }
    
    return {
        'signals': waveform_data,
        'time_slots': time_slots,
        'total_duration': vcd_data['total_time']
    }

def get_signal_color(signal_name: str) -> str:
    """Assign consistent colors to signals"""
    color_map = {
        'clk': '#FF6B6B',
        'a': '#4ECDC4',
        'b': '#FFD166',
        'c': '#06D6A0',
        'd': '#118AB2',
        'out': '#EF476F',
        'sum': '#7209B7',
        'cout': '#F3722C',
        'q': '#277DA1'
    }
    return color_map.get(signal_name, '#6A0572')

def generate_html_waveform_viewer(vcd_file: Path, waveform_id: str) -> Optional[Path]:
    """
    Generate professional HTML/JS waveform viewer
    """
    try:
        # Parse VCD file
        vcd_data = parse_vcd_file(vcd_file)
        waveform_data = generate_waveform_data(vcd_data)
        
        if not waveform_data:
            logger.warning("No waveform data to display")
            return None
        
        # Create HTML file
        html_file = WAVEFORM_DIR / f"{waveform_id}.html"
        
        html_content = create_waveform_html_content(waveform_id, waveform_data)
        
        html_file.write_text(html_content)
        logger.info(f"HTML waveform viewer created: {html_file}")
        
        return html_file
        
    except Exception as e:
        logger.error(f"Failed to create HTML waveform viewer: {e}")
        return None

def create_waveform_html_content(waveform_id: str, waveform_data: Dict) -> str:
    """
    Create HTML content for the waveform viewer
    """
    signals = waveform_data.get('signals', {})
    total_duration = waveform_data.get('total_duration', 100)
    
    # Generate HTML with embedded JavaScript
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Waveform Viewer: {waveform_id}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px 30px;
        }}
        
        .header h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 5px;
        }}
        
        .header .subtitle {{
            font-size: 14px;
            opacity: 0.9;
        }}
        
        .controls {{
            padding: 20px 30px;
            background: #f8f9fa;
            border-bottom: 1px solid #e9ecef;
            display: flex;
            gap: 15px;
            align-items: center;
            flex-wrap: wrap;
        }}
        
        .control-group {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .control-label {{
            font-weight: 600;
            color: #495057;
        }}
        
        .btn {{
            padding: 8px 16px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.3s ease;
        }}
        
        .btn:hover {{
            background: #5a67d8;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        
        .btn-secondary {{
            background: #6c757d;
        }}
        
        .btn-secondary:hover {{
            background: #5a6268;
        }}
        
        .waveform-container {{
            padding: 30px;
            overflow-x: auto;
        }}
        
        .waveform-grid {{
            display: grid;
            grid-template-columns: 120px 1fr;
            gap: 20px;
            min-width: 800px;
        }}
        
        .signal-label {{
            text-align: right;
            padding: 15px 10px;
            font-weight: 600;
            color: #495057;
            border-right: 2px solid #e9ecef;
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 8px;
        }}
        
        .signal-color {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
        }}
        
        .waveform-display {{
            position: relative;
            height: 60px;
            background: #f8f9fa;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #e9ecef;
        }}
        
        .waveform {{
            position: relative;
            height: 100%;
        }}
        
        .wave-segment {{
            position: absolute;
            height: 100%;
            transition: all 0.3s ease;
        }}
        
        .wave-high {{
            background: linear-gradient(135deg, #4CAF50, #45a049);
        }}
        
        .wave-low {{
            background: linear-gradient(135deg, #f44336, #d32f2f);
        }}
        
        .wave-unknown {{
            background: linear-gradient(135deg, #ff9800, #f57c00);
        }}
        
        .wave-x {{
            background: repeating-linear-gradient(
                45deg,
                #9e9e9e,
                #9e9e9e 10px,
                #757575 10px,
                #757575 20px
            );
        }}
        
        .time-scale {{
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-top: 2px solid #e9ecef;
            margin-top: 20px;
            font-size: 12px;
            color: #6c757d;
        }}
        
        .time-marker {{
            position: absolute;
            top: -25px;
            transform: translateX(-50%);
            font-size: 11px;
            color: #6c757d;
            white-space: nowrap;
        }}
        
        .legend {{
            padding: 20px 30px;
            background: #f8f9fa;
            border-top: 1px solid #e9ecef;
            display: flex;
            gap: 30px;
            flex-wrap: wrap;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .legend-color {{
            width: 20px;
            height: 20px;
            border-radius: 4px;
        }}
        
        .footer {{
            padding: 20px 30px;
            text-align: center;
            color: #6c757d;
            font-size: 14px;
            border-top: 1px solid #e9ecef;
        }}
        
        .footer a {{
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
        }}
        
        .footer a:hover {{
            text-decoration: underline;
        }}
        
        .zoom-controls {{
            display: flex;
            gap: 5px;
        }}
        
        .zoom-btn {{
            width: 36px;
            height: 36px;
            border-radius: 6px;
            background: white;
            border: 1px solid #dee2e6;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-weight: bold;
            font-size: 18px;
        }}
        
        .zoom-btn:hover {{
            background: #f8f9fa;
        }}
        
        @media (max-width: 768px) {{
            .waveform-grid {{
                grid-template-columns: 100px 1fr;
                gap: 15px;
            }}
            
            .controls {{
                flex-direction: column;
                align-items: stretch;
            }}
            
            .control-group {{
                justify-content: space-between;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📈 Digital Waveform Viewer</h1>
            <div class="subtitle">Waveform ID: {waveform_id} | Generated: <span id="timestamp"></span></div>
        </div>
        
        <div class="controls">
            <div class="control-group">
                <span class="control-label">Zoom:</span>
                <div class="zoom-controls">
                    <button class="zoom-btn" onclick="zoomOut()">-</button>
                    <button class="zoom-btn" onclick="zoomIn()">+</button>
                    <button class="zoom-btn" onclick="resetZoom()">↺</button>
                </div>
                <span class="control-label" id="zoomLevel">100%</span>
            </div>
            
            <div class="control-group">
                <span class="control-label">View Mode:</span>
                <button class="btn" onclick="toggleViewMode()" id="viewModeBtn">Detailed</button>
            </div>
            
            <div style="flex-grow: 1;"></div>
            
            <div class="control-group">
                <button class="btn btn-secondary" onclick="downloadVCD()">
                    ⬇ Download VCD
                </button>
                <button class="btn" onclick="takeScreenshot()">
                    📸 Screenshot
                </button>
            </div>
        </div>
        
        <div class="waveform-container">
            <div class="waveform-grid" id="waveformGrid">
                <!-- Signals will be inserted here by JavaScript -->
            </div>
            
            <div class="time-scale" id="timeScale">
                <!-- Time markers will be inserted here -->
            </div>
        </div>
        
        <div class="legend">
            <div class="legend-item">
                <div class="legend-color wave-high"></div>
                <span>High (1)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color wave-low"></div>
                <span>Low (0)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color wave-unknown"></div>
                <span>Unknown (x)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color wave-x"></div>
                <span>High-Z (z)</span>
            </div>
        </div>
        
        <div class="footer">
            <p>
                Waveform generated from VCD file. 
                <a href="/waveforms/{waveform_id}.vcd" download>Download original VCD</a> 
                for use with GTKWave or other VCD viewers.
            </p>
            <p style="margin-top: 10px; font-size: 12px;">
                Use mouse wheel to zoom • Drag to pan • Click segments for details
            </p>
        </div>
    </div>
    
    <script>
        // Waveform data from Python
        const waveformData = {json.dumps(waveform_data, indent=2)};
        const waveformId = "{waveform_id}";
        
        // Display settings
        let zoomLevel = 1.0;
        let viewMode = 'detailed'; // 'detailed' or 'compact'
        let timeScale = 50; // pixels per 10ns
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            document.getElementById('timestamp').textContent = new Date().toLocaleString();
            renderWaveforms();
            setupEventListeners();
        }});
        
        function renderWaveforms() {{
            const grid = document.getElementById('waveformGrid');
            const timeScaleDiv = document.getElementById('timeScale');
            
            // Clear previous content
            grid.innerHTML = '';
            timeScaleDiv.innerHTML = '';
            
            const signals = waveformData.signals;
            const totalDuration = waveformData.total_duration || 100;
            
            // Calculate display width
            const displayWidth = Math.max(800, totalDuration * timeScale * zoomLevel / 10);
            
            // Render each signal
            Object.values(signals).forEach(signal => {{
                // Signal label
                const labelDiv = document.createElement('div');
                labelDiv.className = 'signal-label';
                labelDiv.innerHTML = `
                    <div class="signal-color" style="background: ${{signal.color}}"></div>
                    <span>${{signal.name}}</span>
                `;
                grid.appendChild(labelDiv);
                
                // Waveform display
                const displayDiv = document.createElement('div');
                displayDiv.className = 'waveform-display';
                displayDiv.style.width = displayWidth + 'px';
                
                const waveformDiv = document.createElement('div');
                waveformDiv.className = 'waveform';
                
                // Create waveform segments
                signal.waveform.forEach(segment => {{
                    const segmentDiv = document.createElement('div');
                    segmentDiv.className = 'wave-segment';
                    
                    const width = (segment.duration * timeScale * zoomLevel / 10);
                    const left = (segment.start * timeScale * zoomLevel / 10);
                    
                    segmentDiv.style.width = width + 'px';
                    segmentDiv.style.left = left + 'px';
                    
                    // Set color based on value
                    if (segment.value === '1') {{
                        segmentDiv.className += ' wave-high';
                        segmentDiv.title = `High (1) at ${{segment.start}}ns for ${{segment.duration}}ns`;
                    }} else if (segment.value === '0') {{
                        segmentDiv.className += ' wave-low';
                        segmentDiv.title = `Low (0) at ${{segment.start}}ns for ${{segment.duration}}ns`;
                    }} else if (segment.value === 'x') {{
                        segmentDiv.className += ' wave-unknown';
                        segmentDiv.title = `Unknown (x) at ${{segment.start}}ns for ${{segment.duration}}ns`;
                    }} else if (segment.value === 'z') {{
                        segmentDiv.className += ' wave-x';
                        segmentDiv.title = `High-Z (z) at ${{segment.start}}ns for ${{segment.duration}}ns`;
                    }}
                    
                    // Add click event for details
                    segmentDiv.addEventListener('click', function() {{
                        showSegmentDetails(signal.name, segment);
                    }});
                    
                    waveformDiv.appendChild(segmentDiv);
                }});
                
                displayDiv.appendChild(waveformDiv);
                grid.appendChild(displayDiv);
            }});
            
            // Render time scale
            const timeStep = Math.max(10, Math.ceil(totalDuration / 10));
            for (let time = 0; time <= totalDuration; time += timeStep) {{
                const marker = document.createElement('div');
                marker.className = 'time-marker';
                marker.style.left = (time * timeScale * zoomLevel / 10) + 'px';
                marker.textContent = `${{time}}ns`;
                timeScaleDiv.appendChild(marker);
            }}
            
            // Update zoom level display
            document.getElementById('zoomLevel').textContent = Math.round(zoomLevel * 100) + '%';
        }}
        
        function setupEventListeners() {{
            // Mouse wheel zoom
            document.querySelector('.waveform-container').addEventListener('wheel', function(e) {{
                e.preventDefault();
                if (e.deltaY < 0) {{
                    zoomIn();
                }} else {{
                    zoomOut();
                }}
            }});
            
            // Keyboard shortcuts
            document.addEventListener('keydown', function(e) {{
                if (e.ctrlKey || e.metaKey) {{
                    if (e.key === '+') {{
                        e.preventDefault();
                        zoomIn();
                    }} else if (e.key === '-') {{
                        e.preventDefault();
                        zoomOut();
                    }} else if (e.key === '0') {{
                        e.preventDefault();
                        resetZoom();
                    }} else if (e.key === 'd') {{
                        e.preventDefault();
                        downloadVCD();
                    }}
                }}
            }});
        }}
        
        function zoomIn() {{
            zoomLevel = Math.min(zoomLevel * 1.2, 5.0);
            renderWaveforms();
        }}
        
        function zoomOut() {{
            zoomLevel = Math.max(zoomLevel / 1.2, 0.2);
            renderWaveforms();
        }}
        
        function resetZoom() {{
            zoomLevel = 1.0;
            renderWaveforms();
        }}
        
        function toggleViewMode() {{
            viewMode = viewMode === 'detailed' ? 'compact' : 'detailed';
            document.getElementById('viewModeBtn').textContent = 
                viewMode === 'detailed' ? 'Compact' : 'Detailed';
            
            // Adjust time scale based on view mode
            timeScale = viewMode === 'detailed' ? 50 : 25;
            renderWaveforms();
        }}
        
        function showSegmentDetails(signalName, segment) {{
            const detail = `
                Signal: <strong>${{signalName}}</strong><br>
                Value: <strong>${{segment.value}}</strong><br>
                Start: ${{segment.start}}ns<br>
                Duration: ${{segment.duration}}ns<br>
                End: ${{segment.start + segment.duration}}ns
            `;
            
            alert(detail);
        }}
        
        function downloadVCD() {{
            // Create a download link
            const link = document.createElement('a');
            link.href = `/waveforms/${{waveformId}}.vcd`;
            link.download = `${{waveformId}}.vcd`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }}
        
        function takeScreenshot() {{
            html2canvas(document.querySelector('.container')).then(canvas => {{
                const link = document.createElement('a');
                link.download = `waveform-${{waveformId}}.png`;
                link.href = canvas.toDataURL('image/png');
                link.click();
            }});
        }}
    </script>
    
    <!-- Include html2canvas for screenshot functionality -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
</body>
</html>'''
    
    return html

def create_simple_waveform_fallback(waveform_id: str):
    """Create a simple HTML fallback if detailed viewer fails"""
    html_file = WAVEFORM_DIR / f"{waveform_id}.html"
    
    simple_html = f'''<!DOCTYPE html>
<html>
<head>
    <title>Waveform: {waveform_id}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f2f5; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }}
        .info {{ background: #e8f5e9; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .btn {{ display: inline-block; background: #4CAF50; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; margin: 10px 5px; }}
        .btn:hover {{ background: #45a049; }}
        .btn-vcd {{ background: #2196F3; }}
        .btn-vcd:hover {{ background: #1976D2; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Waveform Viewer</h1>
        <div class="info">
            <p><strong>Waveform ID:</strong> {waveform_id}</p>
            <p><strong>Status:</strong> VCD file generated successfully</p>
            <p><strong>File Size:</strong> Loading...</p>
        </div>
        
        <h3>How to view this waveform:</h3>
        <ol>
            <li>Download the VCD file using the button below</li>
            <li>Open it with GTKWave (desktop application)</li>
            <li>Or use any VCD viewer tool</li>
        </ol>
        
        <h3>Quick Actions:</h3>
        <a href="/waveforms/{waveform_id}.vcd" class="btn btn-vcd" download>⬇ Download VCD File</a>
        <a href="/api/waveform/{waveform_id}" class="btn">🔄 Refresh Viewer</a>
        
        <h3>What is a VCD file?</h3>
        <p>VCD (Value Change Dump) is a standard format for digital waveform data. It contains all signal transitions over time.</p>
        
        <h3>Recommended Tools:</h3>
        <ul>
            <li><strong>GTKWave</strong> - Free, open-source waveform viewer</li>
            <li><strong>Sigrok/PulseView</strong> - Professional signal analysis</li>
            <li><strong>ModelSim/QuestaSim</strong> - Industry standard simulators</li>
        </ul>
    </div>
    
    <script>
        // Try to get file size
        fetch('/waveforms/{waveform_id}.vcd', {{ method: 'HEAD' }})
            .then(response => {{
                const size = response.headers.get('content-length');
                if (size) {{
                    document.querySelector('.info p:last-child').innerHTML = 
                        `<strong>File Size:</strong> ${{(size / 1024).toFixed(2)}} KB`;
                }}
            }});
    </script>
</body>
</html>'''
    
    html_file.write_text(simple_html)
    logger.info(f"Created simple fallback HTML: {html_file}")

# ==================== HEALTH & DEBUG ENDPOINTS ====================

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    tools_status = {}
    missing_tools = []
    
    # Check required tools
    for tool in ["iverilog", "vvp"]:
        try:
            result = subprocess.run([tool, "--version"], 
                                  capture_output=True, 
                                  text=True,
                                  timeout=5)
            tools_status[tool] = "available"
            if result.stdout:
                tools_status[f"{tool}_version"] = result.stdout.split('\n')[0]
        except (subprocess.SubprocessError, FileNotFoundError):
            tools_status[tool] = "missing"
            missing_tools.append(tool)
    
    # Check optional tools
    for tool in ["gtkwave", "xvfb-run"]:
        try:
            subprocess.run(["which", tool], 
                         capture_output=True, 
                         timeout=5)
            tools_status[tool] = "available"
        except:
            tools_status[tool] = "optional"
    
    status = "healthy" if "iverilog" in tools_status and tools_status["iverilog"] == "available" else "degraded"
    
    return {
        "status": status,
        "tools": tools_status,
        "missing_tools": missing_tools,
        "waveform_dir": str(WAVEFORM_DIR),
        "problems_count": len(PROBLEMS),
        "waveform_files": list(WAVEFORM_DIR.glob("*"))
    }

@app.get("/api/debug/waveforms")
async def debug_waveforms():
    """Debug endpoint to list all waveform files"""
    files = []
    for file in WAVEFORM_DIR.glob("*"):
        files.append({
            "name": file.name,
            "size": file.stat().st_size,
            "modified": file.stat().st_mtime,
            "url": f"/waveforms/{file.name}"
        })
    return {"files": files}

@app.delete("/api/waveforms")
async def cleanup_waveforms():
    """Cleanup all waveform files"""
    try:
        deleted = 0
        for file in WAVEFORM_DIR.glob("*"):
            if file.is_file():
                file.unlink()
                deleted += 1
        return {"message": f"Deleted {deleted} waveform files"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== CLEANUP ====================

import time
import asyncio

async def cleanup_old_wavefiles():
    """Cleanup old waveform files (older than 1 hour)"""
    try:
        current_time = time.time()
        deleted = 0
        for file in WAVEFORM_DIR.glob("*"):
            if file.is_file():
                file_age = current_time - file.stat().st_mtime
                if file_age > 3600:  # 1 hour
                    try:
                        file.unlink()
                        deleted += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete {file}: {e}")
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old waveform files")
    except Exception as e:
        logger.error(f"Error in cleanup: {e}")

@app.on_event("startup")
async def startup_event():
    """Cleanup on startup and schedule periodic cleanup"""
    await cleanup_old_wavefiles()
    # Schedule periodic cleanup every hour
    asyncio.create_task(periodic_cleanup())

async def periodic_cleanup():
    """Periodic cleanup task"""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        await cleanup_old_wavefiles()

# ==================== MAIN ====================

import os
PORT = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on port {PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )
