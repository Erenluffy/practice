#!/usr/bin/env python3
"""
Backend API for VLSI Practice
Run on your VPS: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
from typing import Dict, Any

app = FastAPI(title="VLSI Practice API")

# CORS - ALLOW YOUR STATIC SITE
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Local dev
        "https://your-static-site.com",  # Your real domain
        "*"  # For testing, restrict in production
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Models
class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"  # Optional

# Load problems
with open("problems.json", "r") as f:
    PROBLEMS = json.load(f)

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "1.0"}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    return {"problems": PROBLEMS}

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """
    Execute Verilog code and return results
    Called from your static website
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
    result = run_simulation(request.code, problem["testbench"])
    
    return {
        "success": result["success"],
        "problem": problem["title"],
        "output": result.get("output", ""),
        "error": result.get("error", ""),
        "hint": problem.get("hint", "") if not result["success"] else ""
    }

def run_simulation(user_code: str, testbench: str) -> Dict[str, Any]:
    """Run iverilog simulation and return results"""
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # Write user code
            design_file = os.path.join(tmpdir, "design.v")
            with open(design_file, "w") as f:
                f.write(user_code)
            
            # Write testbench
            tb_file = os.path.join(tmpdir, "tb.v")
            with open(tb_file, "w") as f:
                f.write(testbench)
            
            # Compile
            output_file = os.path.join(tmpdir, "sim")
            compile_cmd = ["iverilog", "-o", output_file, design_file, tb_file]
            
            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Compilation failed: {compile_result.stderr[:500]}"
                }
            
            # Simulate
            sim_result = subprocess.run(
                ["vvp", output_file],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            output = sim_result.stdout + sim_result.stderr
            
            return {
                "success": "PASS" in output or "SUCCESS" in output,
                "output": output[:1000],  # Limit size
                "error": "" if "PASS" in output else "Tests failed"
            }
            
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout - Code took too long"}
        except Exception as e:
            return {"success": False, "error": f"System error: {str(e)}"}
# Cloud deployment optimizations
import os
PORT = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=PORT,
        # Optimize for cloud
        access_log=False,
        timeout_keep_alive=30,
        limit_concurrency=100
    )
