#!/usr/bin/env python3
"""
Backend API for VLSI Practice with Waveform Generation
Run on your VPS: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import shutil
import uuid
from typing import Dict, Any
from pathlib import Path

app = FastAPI(title="VLSI Practice API")

# Create waveform directory
WAVEFORM_DIR = Path("/app/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True)

# CORS - ALLOW YOUR STATIC SITE
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Local dev
        "http://localhost:8080",  # Local frontend
        "https://your-static-site.com",  # Your real domain
        "*"  # For testing, restrict in production
    ],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Models
class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"  # Optional
    generate_waveform: bool = False  # New: whether to generate waveform

# Load problems
with open("problems.json", "r") as f:
    PROBLEMS = json.load(f)

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "2.0", "features": ["iverilog", "gtkwave", "vcd_generation"]}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    return {"problems": PROBLEMS}

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str):
    """Serve waveform files"""
    waveform_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
    svg_path = WAVEFORM_DIR / f"{waveform_id}.svg"
    
    if svg_path.exists():
        return FileResponse(svg_path, media_type="image/svg+xml")
    elif waveform_path.exists():
        return FileResponse(waveform_path, media_type="application/octet-stream")
    else:
        raise HTTPException(status_code=404, detail="Waveform not found")

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """
    Execute Verilog code and return results with optional waveform
    """
    # 1. Find the problem
    problem = None
    for p in PROBLEMS:
        if p["id"] == request.problem_id:
            problem = p
            break
    
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    
    # 2. Run simulation
    result = run_real_simulation(
        request.code, 
        problem["testbench"], 
        generate_waveform=request.generate_waveform
    )
    
    response = {
        "success": result["success"],
        "problem": problem["title"],
        "output": result.get("output", ""),
        "error": result.get("error", ""),
        "hint": problem.get("hint", "") if not result["success"] else ""
    }
    
    # Add waveform info if generated
    if "waveform_id" in result:
        response["waveform_id"] = result["waveform_id"]
        response["waveform_url"] = f"/api/waveform/{result['waveform_id']}"
    
    return response

def run_real_simulation(user_code: str, testbench_code: str, generate_waveform: bool = False) -> dict:
    """
    Takes user's Verilog module and the hidden testbench,
    compiles and simulates using Icarus Verilog, returns results and optional waveform.
    """
    waveform_id = None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # 1. Modify testbench to dump VCD if waveform is requested
        if generate_waveform:
            # Add $dumpfile and $dumpvars to testbench
            modified_testbench = modify_testbench_for_waveform(testbench_code)
            waveform_id = str(uuid.uuid4())
            combined_source = f"""
`timescale 1ns/1ps
{user_code}
{modified_testbench.format(waveform_file=str(tmpdir_path / "waveform.vcd"))}
            """
        else:
            combined_source = f"""
`timescale 1ns/1ps
{user_code}
{testbench_code}
            """
        
        source_file = tmpdir_path / "design.v"
        source_file.write_text(combined_source)
        
        # 2. Compile with Icarus Verilog
        output_executable = tmpdir_path / "sim"
        compile_cmd = ["iverilog", "-o", str(output_executable), str(source_file)]
        
        try:
            # Compile step
            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=15
            )
            
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "error": "Compilation Failed",
                    "details": compile_result.stderr,
                    "type": "compilation"
                }
            
            # 3. Simulate with vvp
            sim_result = subprocess.run(
                ["vvp", str(output_executable)],
                capture_output=True,
                text=True,
                timeout=15
            )
            
            # 4. Parse the simulation output
            output = sim_result.stdout
            
            if "PASS" in output:
                result = {"success": True, "output": output}
                
                # 5. Generate waveform if requested
                if generate_waveform and waveform_id:
                    waveform_file = tmpdir_path / "waveform.vcd"
                    if waveform_file.exists():
                        # Copy VCD to persistent storage
                        dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                        shutil.copy2(waveform_file, dest_vcd)
                        
                        # Generate SVG from VCD using gtkwave
                        try:
                            svg_file = generate_svg_from_vcd(waveform_file, waveform_id)
                            if svg_file:
                                result["waveform_svg"] = True
                        except Exception as e:
                            print(f"SVG generation failed: {e}")
                            # Still provide VCD
                            pass
                        
                        result["waveform_id"] = waveform_id
                
                return result
            else:
                # Extract error message
                error_line = next((line for line in output.split('\n') if "FAIL" in line), 
                                "Simulation output did not contain PASS.")
                return {
                    "success": False,
                    "error": "Test Failed",
                    "details": error_line,
                    "output": output
                }
                
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout", "details": "Simulation took too long."}

def modify_testbench_for_waveform(testbench_code: str) -> str:
    """
    Modify testbench to include $dumpfile and $dumpvars commands
    """
    # Find the initial block
    if "initial begin" in testbench_code:
        # Insert dump commands after initial begin
        modified = testbench_code.replace(
            "initial begin",
            "initial begin\n    $dumpfile(\"{waveform_file}\");\n    $dumpvars(0);"
        )
    else:
        # Create initial block if not present
        modified = testbench_code
        if "module" in modified and "endmodule" in modified:
            # Insert after module declaration but before any other code
            module_end = modified.find(");") + 2
            if module_end > 1:
                modified = modified[:module_end] + "\n    initial begin\n        $dumpfile(\"{waveform_file}\");\n        $dumpvars(0);\n    end" + modified[module_end:]
    
    return modified

def generate_svg_from_vcd(vcd_file: Path, waveform_id: str) -> Path:
    """
    Generate SVG waveform from VCD using gtkwave
    """
    tcl_script = WAVEFORM_DIR / f"{waveform_id}.tcl"
    svg_file = WAVEFORM_DIR / f"{waveform_id}.svg"
    
    # Create TCL script for gtkwave
    tcl_content = f """
# Load VCD file
gtkwave::loadFile "{vcd_file}"

# Set time range
gtkwave::setZoomFactor -5

# Add all signals
set all_signals [gtkwave::getSignals "*"]
gtkwave::addSignalsFromList $all_signals

# Configure SVG export
gtkwave::/Edit/Set_Theme/Classic
gtkwave::/View/Show_Grid/On
gtkwave::/View/Show_Axis/On

# Export to SVG
gtkwave::/File/Export_To_SVG "{svg_file}" -flatten

# Exit
gtkwave::quit
"""
    
    tcl_script.write_text(tcl_content)
    
    try:
        # Run gtkwave in batch mode with Xvfb (virtual display)
        subprocess.run([
            "xvfb-run", "-a", "gtkwave", 
            "-f", str(vcd_file),
            "-T", str(tcl_script)
        ], capture_output=True, text=True, timeout=30)
        
        if svg_file.exists():
            return svg_file
    except Exception as e:
        print(f"GTKWave error: {e}")
    
    return None

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    # Test if iverilog and gtkwave are available
    tools_available = True
    missing_tools = []
    
    for tool in ["iverilog", "vvp", "gtkwave", "xvfb-run"]:
        try:
            subprocess.run([tool, "--version"], capture_output=True, timeout=5)
        except (subprocess.SubprocessError, FileNotFoundError):
            tools_available = False
            missing_tools.append(tool)
    
    return {
        "status": "healthy" if tools_available else "degraded",
        "tools_available": tools_available,
        "missing_tools": missing_tools,
        "waveform_dir": str(WAVEFORM_DIR),
        "problems_count": len(PROBLEMS)
    }

# Clean old waveform files periodically (older than 1 hour)
import time
def cleanup_old_waveforms():
    """Cleanup old waveform files"""
    current_time = time.time()
    for file in WAVEFORM_DIR.glob("*"):
        if file.is_file():
            file_age = current_time - file.stat().st_mtime
            if file_age > 3600:  # 1 hour
                try:
                    file.unlink()
                except:
                    pass

@app.on_event("startup")
async def startup_event():
    """Cleanup on startup"""
    cleanup_old_waveforms()

import os
PORT = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=PORT,
        access_log=False,
        timeout_keep_alive=30,
    )
