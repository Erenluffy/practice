#!/usr/bin/env python3
"""
Backend API for VLSI Practice with Waveform Generation
Run on your VPS: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse
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

# === FIX: Use consistent waveform directory ===
WAVEFORM_DIR = Path("/app/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

# Log the waveform directory for debugging
logger.info(f"Waveform directory: {WAVEFORM_DIR}")
logger.info(f"Waveform directory exists: {WAVEFORM_DIR.exists()}")

# === FIX: Mount static files at root path ===
try:
    app.mount("/waveforms", StaticFiles(directory=WAVEFORM_DIR), name="waveforms")
    logger.info(f"Static files mounted at /waveforms")
except Exception as e:
    logger.error(f"Failed to mount static files: {e}")

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
        "https://vlsi-practice-api.onrender.com",  # Add your own domain
        "https://vlsi-playground.onrender.com",    # Add frontend domain
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
        logger.info(f"Waveform directory: {WAVEFORM_DIR}")
        logger.info(f"Files in directory: {list(WAVEFORM_DIR.glob('*'))}")
        
        # Clean up any invalid characters
        waveform_id = waveform_id.strip()
        if not waveform_id:
            raise HTTPException(status_code=400, detail="Invalid waveform ID")
        
        # Check for HTML viewer first
        html_path = WAVEFORM_DIR / f"{waveform_id}.html"
        svg_path = WAVEFORM_DIR / f"{waveform_id}.svg"
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        logger.info(f"Checking HTML path: {html_path} - exists: {html_path.exists()}")
        logger.info(f"Checking SVG path: {svg_path} - exists: {svg_path.exists()}")
        logger.info(f"Checking VCD path: {vcd_path} - exists: {vcd_path.exists()}")
        
        if html_path.exists():
            logger.info(f"Serving HTML viewer: {html_path}")
            # Read and return HTML content directly
            html_content = html_path.read_text()
            return HTMLResponse(content=html_content)
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
            # List available files for debugging
            available_files = [f.name for f in WAVEFORM_DIR.glob("*")]
            raise HTTPException(
                status_code=404, 
                detail=f"Waveform {waveform_id} not found. Available files: {available_files}"
            )
            
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
            if result.get("waveform_svg", False):
                response["waveform_svg_url"] = f"/waveforms/{result['waveform_id']}.svg"
        
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
                
                # 7. Generate waveform if requested - FIXED THIS SECTION
                if generate_waveform and waveform_id:
                    waveform_file = tmpdir_path / "waveform.vcd"
                    
                    logger.info(f"Looking for VCD file at: {waveform_file}")
                    logger.info(f"VCD file exists: {waveform_file.exists()}")
                    
                    if waveform_file.exists():
                        file_size = waveform_file.stat().st_size
                        logger.info(f"VCD file size: {file_size} bytes")
                        
                        if file_size > 0:
                            # Copy VCD to persistent storage
                            dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                            try:
                                # Ensure directory exists
                                WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)
                                
                                # Copy the file
                                shutil.copy2(waveform_file, dest_vcd)
                                
                                # Verify copy
                                if dest_vcd.exists():
                                    copied_size = dest_vcd.stat().st_size
                                    logger.info(f"✅ Waveform saved: {dest_vcd} ({copied_size} bytes)")
                                    logger.info(f"✅ VCD file copied successfully")
                                    
                                    # Create HTML viewer
                                    html_file = create_simple_waveform_viewer(waveform_id)
                                    if html_file and html_file.exists():
                                        result["waveform_html"] = True
                                        logger.info(f"✅ HTML viewer created: {html_file}")
                                    
                                    result["waveform_id"] = waveform_id
                                    logger.info(f"✅ Waveform ID: {waveform_id}")
                                    
                                    # DEBUG: List files in waveform directory
                                    logger.info(f"Files in {WAVEFORM_DIR}: {list(WAVEFORM_DIR.glob('*'))}")
                                else:
                                    logger.error(f"❌ Failed to copy VCD file: {dest_vcd} does not exist")
                            except Exception as e:
                                logger.error(f"❌ Failed to save waveform: {e}")
                        else:
                            logger.warning(f"VCD file exists but is empty (0 bytes)")
                    else:
                        # Check if VCD was created with a different name
                        vcd_files = list(tmpdir_path.glob("*.vcd"))
                        logger.warning(f"No waveform.vcd found. Other VCD files: {vcd_files}")
                        
                        if vcd_files:
                            # Try the first VCD file found
                            alt_vcd = vcd_files[0]
                            logger.info(f"Trying alternative VCD file: {alt_vcd}")
                            
                            dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                            shutil.copy2(alt_vcd, dest_vcd)
                            
                            if dest_vcd.exists():
                                # Create HTML viewer
                                html_file = create_simple_waveform_viewer(waveform_id)
                                if html_file:
                                    result["waveform_html"] = True
                                
                                result["waveform_id"] = waveform_id
                                logger.info(f"✅ Waveform saved from alternative file")
                
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
            logger.error(f"Simulation error: {e}")
            return {
                "success": False,
                "error": "Simulation Error",
                "details": str(e)
            }

def create_simple_waveform_viewer(waveform_id: str) -> Optional[Path]:
    """Create a simple HTML waveform viewer"""
    try:
        html_file = WAVEFORM_DIR / f"{waveform_id}.html"
        
        html_content = f'''<!DOCTYPE html>
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
        .wave-preview {{ margin: 20px 0; padding: 20px; background: #f8f9fa; border-radius: 5px; }}
        .signal {{ display: flex; align-items: center; margin: 10px 0; }}
        .signal-name {{ width: 100px; font-weight: bold; }}
        .wave {{ display: flex; flex: 1; height: 30px; background: white; border: 1px solid #ddd; }}
        .high {{ background: #4CAF50; }}
        .low {{ background: #f44336; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Waveform Viewer</h1>
        <div class="info">
            <p><strong>Waveform ID:</strong> {waveform_id}</p>
            <p><strong>Status:</strong> VCD file generated successfully</p>
            <p><strong>File Type:</strong> Value Change Dump (VCD)</p>
        </div>
        
        <h3>Quick Actions:</h3>
        <a href="/waveforms/{waveform_id}.vcd" class="btn btn-vcd" download>
            <i class="fas fa-download"></i> Download VCD File
        </a>
        <button class="btn" onclick="window.location.reload()">
            <i class="fas fa-redo"></i> Refresh
        </button>
        
        <h3>How to view:</h3>
        <ol>
            <li>Download the VCD file using the button above</li>
            <li>Open with GTKWave (desktop application)</li>
            <li>Or use online VCD viewers</li>
        </ol>
        
        <h3>Preview:</h3>
        <div class="wave-preview">
            <p><em>Simple digital waveform preview:</em></p>
            <div class="signal">
                <div class="signal-name">a</div>
                <div class="wave">
                    <div class="low" style="width: 25%;"></div>
                    <div class="high" style="width: 25%;"></div>
                    <div class="low" style="width: 25%;"></div>
                    <div class="high" style="width: 25%;"></div>
                </div>
            </div>
            <div class="signal">
                <div class="signal-name">b</div>
                <div class="wave">
                    <div class="high" style="width: 25%;"></div>
                    <div class="low" style="width: 25%;"></div>
                    <div class="high" style="width: 25%;"></div>
                    <div class="low" style="width: 25%;"></div>
                </div>
            </div>
            <div class="signal">
                <div class="signal-name">out</div>
                <div class="wave">
                    <div class="low" style="width: 50%;"></div>
                    <div class="high" style="width: 50%;"></div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Add Font Awesome for icons
        const faLink = document.createElement('link');
        faLink.rel = 'stylesheet';
        faLink.href = 'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css';
        document.head.appendChild(faLink);
    </script>
</body>
</html>'''
        
        html_file.write_text(html_content)
        logger.info(f"Created simple HTML viewer: {html_file}")
        return html_file
        
    except Exception as e:
        logger.error(f"Failed to create HTML viewer: {e}")
        return None

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
        "waveform_files": [f.name for f in WAVEFORM_DIR.glob("*")],
        "problems_count": len(PROBLEMS)
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
            "url": f"/waveforms/{file.name}",
            "api_url": f"/api/waveform/{file.stem}"
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

# Clean old waveform files periodically
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

import os
PORT = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on port {PORT}")
    logger.info(f"Waveform directory: {WAVEFORM_DIR}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )
