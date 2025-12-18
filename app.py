#!/usr/bin/env python3
"""
Backend API for VLSI Practice with Waveform Generation
Run on your VPS: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import shutil
import uuid
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

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
    """Serve waveform files"""
    try:
        logger.info(f"Requesting waveform: {waveform_id}")
        
        # Check for SVG first
        svg_path = WAVEFORM_DIR / f"{waveform_id}.svg"
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        if svg_path.exists():
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
            raise HTTPException(status_code=404, detail="Waveform not found or expired")
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
                
                # 7. Generate waveform if requested
                if generate_waveform and waveform_id:
                    waveform_file = tmpdir_path / "waveform.vcd"
                    if waveform_file.exists() and waveform_file.stat().st_size > 0:
                        # Copy VCD to persistent storage
                        dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                        shutil.copy2(waveform_file, dest_vcd)
                        logger.info(f"Waveform saved: {dest_vcd} ({dest_vcd.stat().st_size} bytes)")
                        
                        # Try to generate SVG - but don't block if it fails
                        try:
                            # Run SVG generation in background to avoid timeout
                            svg_file = generate_svg_from_vcd_async(waveform_file, waveform_id)
                            if svg_file:
                                result["waveform_svg"] = True
                        except Exception as e:
                            logger.warning(f"SVG generation failed (VCD still available): {e}")
                            # Don't fail the whole request if SVG generation fails
                        
                        result["waveform_id"] = waveform_id
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

def generate_svg_from_vcd_async(vcd_file: Path, waveform_id: str) -> Optional[Path]:
    """
    Generate SVG waveform from VCD using gtkwave - runs asynchronously
    """
    svg_file = WAVEFORM_DIR / f"{waveform_id}.svg"
    
    # Create a simplified TCL script that exits quickly
    tcl_content = f'''\
gtkwave::loadFile "{vcd_file}"
set all_signals [gtkwave::getSignals "*"]
gtkwave::addSignalsFromList $all_signals
gtkwave::setZoomFactor -5
gtkwave::/File/Export_To_SVG "{svg_file}"
exit
'''
    
    # Write TCL script
    tcl_file = WAVEFORM_DIR / f"{waveform_id}.tcl"
    tcl_file.write_text(tcl_content)
    
    try:
        # Use a simpler approach with shorter timeout
        cmd = []
        
        # Check DISPLAY environment
        if not os.environ.get("DISPLAY"):
            # No display available, try xvfb
            try:
                subprocess.run(["which", "xvfb-run"], capture_output=True, check=True)
                cmd = ["xvfb-run", "-a"]
            except:
                logger.warning("No DISPLAY and xvfb-run not available")
                return None
        
        cmd.extend(["gtkwave", "-f", str(vcd_file), "-T", str(tcl_file)])
        
        logger.info(f"Running GTKWave for SVG generation: {' '.join(cmd)}")
        
        # Use a shorter timeout
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15  # Shorter timeout
        )
        
        if result.returncode == 0 and svg_file.exists() and svg_file.stat().st_size > 100:
            logger.info(f"SVG generated successfully: {svg_file}")
            return svg_file
        else:
            logger.warning(f"GTKWave failed or no SVG created: {result.stderr}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.warning("GTKWave timeout - SVG generation taking too long")
        return None
    except Exception as e:
        logger.warning(f"SVG generation error: {e}")
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
        "problems_count": len(PROBLEMS),
        "waveform_files": len(list(WAVEFORM_DIR.glob("*.vcd"))) + len(list(WAVEFORM_DIR.glob("*.svg")))
    }

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
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )
