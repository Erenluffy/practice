#!/usr/bin/env python3
"""
Backend API for VLSI Practice with Waveform Generation
Optimized for Render.com deployment
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import shutil
import uuid
import logging
from typing import Dict, Any
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

# Use /tmp/waveforms for Render.com (ephemeral storage)
WAVEFORM_DIR = Path("/tmp/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"Waveform directory: {WAVEFORM_DIR}")
logger.info(f"Directory exists: {WAVEFORM_DIR.exists()}")

# CORS - Allow frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all for testing
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
    return {"status": "VLSI Practice API", "version": "2.0"}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
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
    
    return {"problems": simplified_problems}

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str):
    """Serve waveform files"""
    try:
        # Check for HTML first, then VCD
        html_path = WAVEFORM_DIR / f"{waveform_id}.html"
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        if html_path.exists():
            content = html_path.read_text()
            return HTMLResponse(content=content)
        elif vcd_path.exists():
            return FileResponse(
                vcd_path,
                media_type="application/octet-stream",
                filename=f"{waveform_id}.vcd"
            )
        else:
            raise HTTPException(status_code=404, detail=f"Waveform {waveform_id} not found")
            
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/waveforms/{filename}")
async def serve_waveform_file(filename: str):
    """Direct file access endpoint"""
    file_path = WAVEFORM_DIR / filename
    
    logger.info(f"Requested file: {filename}")
    logger.info(f"Full path: {file_path}")
    logger.info(f"File exists: {file_path.exists()}")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found")
    
    if filename.endswith('.html'):
        content = file_path.read_text()
        return HTMLResponse(content=content)
    elif filename.endswith('.vcd'):
        return FileResponse(
            file_path,
            media_type="application/octet-stream",
            filename=filename
        )
    else:
        return FileResponse(file_path)

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
            request.generate_waveform
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
            response["waveform_vcd_url"] = f"/waveforms/{waveform_id}.vcd"
            response["waveform_html_url"] = f"/waveforms/{waveform_id}.html"
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def run_simulation(user_code: str, testbench: str, generate_waveform: bool) -> Dict:
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
                    shutil.copy2(vcd_file, dest_vcd)
                    
                    # Create HTML viewer
                    create_html_viewer(waveform_id)
                    
                    result["waveform_id"] = waveform_id
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def create_html_viewer(waveform_id: str):
    """Create HTML viewer for waveform"""
    html_path = WAVEFORM_DIR / f"{waveform_id}.html"
    
    html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>Waveform {waveform_id}</title>
    <style>
        body {{ font-family: Arial, sans-serif; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        h1 {{ color: #333; }}
        .info {{ background: #e8f5e9; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .btn {{ display: inline-block; background: #4CAF50; color: white; padding: 10px 20px; 
                text-decoration: none; border-radius: 5px; margin: 10px 5px; }}
        .btn-vcd {{ background: #2196F3; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Waveform Viewer</h1>
        <div class="info">
            <p><strong>ID:</strong> {waveform_id}</p>
            <p><strong>Status:</strong> Ready to download</p>
        </div>
        
        <a href="/waveforms/{waveform_id}.vcd" class="btn btn-vcd" download>
            Download VCD File
        </a>
        <a href="/api/waveform/{waveform_id}" class="btn">
            View in API
        </a>
        
        <h3>How to view:</h3>
        <ol>
            <li>Download the VCD file</li>
            <li>Open with GTKWave or similar viewer</li>
            <li>Or use online VCD viewers</li>
        </ol>
    </div>
</body>
</html>'''
    
    html_path.write_text(html_content)
    logger.info(f"Created HTML viewer: {html_path}")

@app.get("/api/health")
async def health_check():
    """Health check"""
    return {
        "status": "healthy",
        "waveform_dir": str(WAVEFORM_DIR),
        "files": [f.name for f in WAVEFORM_DIR.glob("*")][:10]
    }

@app.get("/api/debug/files")
async def debug_files():
    """Debug endpoint"""
    files = []
    for f in WAVEFORM_DIR.glob("*"):
        files.append({
            "name": f.name,
            "size": f.stat().st_size,
            "url": f"/waveforms/{f.name}"
        })
    return {"files": files}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
