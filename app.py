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


def run_real_simulation(user_code: str, testbench_code: str) -> dict:
    """
    Takes user's Verilog module and the hidden testbench,
    compiles and simulates using Icarus Verilog, and returns the results.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Create the single source file
        # The testbench instantiates the user's 'top_module'
        combined_source = f"""
`timescale 1ns/1ps
{user_code}
{testbench_code}
        """
        
        source_file = os.path.join(tmpdir, "design.v")
        with open(source_file, 'w') as f:
            f.write(combined_source)
        
        # 2. Compile with Icarus Verilog
        output_executable = os.path.join(tmpdir, "sim")
        compile_cmd = ["iverilog", "-o", output_executable, source_file]
        
        try:
            # Compile step
            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=10
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
                ["vvp", output_executable],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            # 4. Parse the simulation output
            # The testbench uses $display("PASS") or $display("FAIL: ...")
            output = sim_result.stdout
            
            if "PASS" in output:
                return {"success": True, "output": output}
            else:
                # Extract error message if any
                error_line = next((line for line in output.split('\n') if "FAIL" in line), "Simulation output did not contain PASS.")
                return {
                    "success": False,
                    "error": "Test Failed",
                    "details": error_line,
                    "output": output
                }
                
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout", "details": "Simulation took too long."}
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
