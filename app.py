#!/usr/bin/env python3
"""
Backend API for VLSI Practice - SystemVerilog Support
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any
import shutil

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API - SystemVerilog Support")

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
    language: str = "verilog"  # "verilog" or "systemverilog"

class TestResult(BaseModel):
    success: bool
    output: str
    error: str = ""
    hint: str = ""
    details: Dict[str, Any] = {}
    compile_log: str = ""
    run_log: str = ""

# Load all problems from separate files
def load_all_problems():
    """Load problems from all problem files"""
    all_problems = []
    
    # Load from problems.json (if exists)
    if os.path.exists("problems.json"):
        try:
            with open("problems.json", "r") as f:
                all_problems.extend(json.load(f))
            logger.info(f"Loaded {len(all_problems)} problems from problems.json")
        except Exception as e:
            logger.error(f"Failed to load problems.json: {e}")
    
    # Load from verilog_problems.json
    if os.path.exists("verilog_problems.json"):
        try:
            with open("verilog_problems.json", "r") as f:
                verilog_problems = json.load(f)
                all_problems.extend(verilog_problems)
            logger.info(f"Loaded {len(verilog_problems)} problems from verilog_problems.json")
        except Exception as e:
            logger.error(f"Failed to load verilog_problems.json: {e}")
    
    # Load from sv_problems.json
    if os.path.exists("sv_problems.json"):
        try:
            with open("sv_problems.json", "r") as f:
                sv_problems = json.load(f)
                all_problems.extend(sv_problems)
            logger.info(f"Loaded {len(sv_problems)} problems from sv_problems.json")
        except Exception as e:
            logger.error(f"Failed to load sv_problems.json: {e}")
    
    # Ensure each problem has a language field
    for problem in all_problems:
        if 'language' not in problem:
            problem['language'] = 'verilog'  # default
    
    logger.info(f"Total problems loaded: {len(all_problems)}")
    return all_problems

# Load all problems
ALL_PROBLEMS = load_all_problems()

@app.get("/")
async def root():
    return {"status": "VLSI Practice API - SystemVerilog Support", "version": "5.0"}

@app.get("/api/problems")
async def get_all_problems():
    """Return all problems (mixed Verilog and SystemVerilog)"""
    simplified = []
    for problem in ALL_PROBLEMS:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"],
            "language": problem.get("language", "verilog"),
            "features": problem.get("features", [])
        })
    return {"problems": simplified}

@app.get("/api/svproblems")
async def get_sv_problems():
    """Return list of SystemVerilog problems"""
    sv_problems = []
    if os.path.exists("sv_problems.json"):
        with open("sv_problems.json", "r") as f:
            sv_problems = json.load(f)
    else:
        # Fallback to filter from ALL_PROBLEMS
        sv_problems = [p for p in ALL_PROBLEMS if p.get("language", "").lower() == "systemverilog"]
    
    simplified = []
    for problem in sv_problems:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"],
            "language": problem.get("language", "systemverilog"),
            "features": problem.get("features", []),
            "constraints": problem.get("constraints", ""),
            "assertions": problem.get("assertions", "")
        })
    return {"problems": simplified}

@app.get("/api/verilog/problems")
async def get_verilog_problems():
    """Return list of Verilog-only problems"""
    verilog_problems = []
    if os.path.exists("verilog_problems.json"):
        with open("verilog_problems.json", "r") as f:
            verilog_problems = json.load(f)
    else:
        # Fallback to filter from ALL_PROBLEMS
        verilog_problems = [p for p in ALL_PROBLEMS if p.get("language", "").lower() != "systemverilog"]
    
    simplified = []
    for problem in verilog_problems:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"],
            "language": "verilog"
        })
    return {"problems": simplified}

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """Execute Verilog/SystemVerilog code"""
    try:
        # Find problem in ALL_PROBLEMS
        problem = next((p for p in ALL_PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Determine language from problem
        language = problem.get("language", "verilog").lower()
        
        # Log the request
        logger.info(f"Running {language} simulation for problem: {problem['title']}")
        
        # Run simulation
        if language == "systemverilog":
            result = run_systemverilog_simulation(
                request.code,
                problem["testbench"],
                problem.get("constraints", ""),
                problem.get("assertions", ""),
                problem.get("features", [])
            )
        else:
            result = run_verilog_simulation(
                request.code,
                problem["testbench"]
            )
        
        # Prepare response
        response = {
            "success": result["success"],
            "problem": problem["title"],
            "language": language,
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", {}),
            "compile_log": result.get("compile_log", ""),
            "run_log": result.get("run_log", ""),
        }
        
        # Add SV-specific fields
        if language == "systemverilog":
            response["assertion_results"] = result.get("assertion_results", [])
            response["coverage"] = result.get("coverage", {})
        
        if not result["success"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def run_verilog_simulation(user_code: str, testbench: str) -> dict:
    """Run standard Verilog simulation using Icarus Verilog"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Combine source
        source = f"`timescale 1ns/1ps\n{user_code}\n{testbench}"
        source_file = tmp_path / "design.v"
        source_file.write_text(source)
        
        # Compile with Icarus Verilog
        output_exec = tmp_path / "sim"
        compile_result = subprocess.run(
            ["iverilog", "-o", str(output_exec), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            return {
                "success": False,
                "error": "Compilation Failed",
                "compile_log": compile_log,
                "details": {"compile_error": True}
            }
        
        # Simulate
        sim_result = subprocess.run(
            ["vvp", str(output_exec)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        run_log = sim_result.stdout + sim_result.stderr
        
        if "PASS" in run_log:
            return {
                "success": True,
                "output": run_log,
                "compile_log": compile_log,
                "run_log": run_log,
                "details": {"simulation_completed": True}
            }
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": run_log[:1000],
                "compile_log": compile_log,
                "run_log": run_log,
                "details": {"test_failed": True}
            }

def run_systemverilog_simulation(user_code: str, testbench: str, constraints: str = "", 
                                assertions: str = "", features: list = None) -> dict:
    """Run SystemVerilog simulation using Verilator"""
    if features is None:
        features = []
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Prepare SystemVerilog source with features
        source_parts = []
        
        # Add timescale
        source_parts.append("`timescale 1ns/1ps")
        
        # Add SystemVerilog features based on problem requirements
        if "assertions" in features:
            source_parts.append("`define SYSTEMVERILOG_ASSERTIONS")
        
        if "coverage" in features:
            source_parts.append("`define SYSTEMVERILOG_COVERAGE")
        
        if "constraints" in features and constraints:
            source_parts.append(constraints)
        
        # Add user code
        source_parts.append(user_code)
        
        # Add assertions if provided
        if assertions and "assertions" in features:
            source_parts.append(assertions)
        
        # Add testbench
        source_parts.append(testbench)
        
        # Combine all parts
        source = "\n\n".join(source_parts)
        source_file = tmp_path / "design.sv"
        source_file.write_text(source)
        
        # Prepare Verilator command
        verilator_cmd = [
            "verilator",
            "--cc",  # Create C++ output
            "--exe",  # Create executable
            "--build",  # Build the executable
            "-Wno-fatal",  # Don't stop on first error
        ]
        
        # Add feature-specific flags
        if "assertions" in features:
            verilator_cmd.append("--assert")
        
        if "coverage" in features:
            verilator_cmd.append("--coverage")
        
        # Add the source file
        verilator_cmd.append(str(source_file))
        
        # Add a simple main.cpp for Verilator
        main_cpp = """
#include "Vdesign.h"
#include "verilated.h"
#include <iostream>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vdesign* top = new Vdesign;
    
    // Reset
    top->rst_n = 0;
    top->clk = 0;
    top->eval();
    
    // Release reset
    for (int i = 0; i < 5; i++) {
        top->clk = !top->clk;
        top->eval();
    }
    top->rst_n = 1;
    
    // Run simulation for 100 cycles
    for (int i = 0; i < 100; i++) {
        top->clk = !top->clk;
        top->eval();
        
        // Check for finish
        if (Verilated::gotFinish()) {
            std::cout << "Simulation finished early" << std::endl;
            break;
        }
    }
    
    top->final();
    delete top;
    
    std::cout << "SystemVerilog simulation completed" << std::endl;
    return 0;
}
"""
        main_file = tmp_path / "main.cpp"
        main_file.write_text(main_cpp)
        verilator_cmd.append(str(main_file))
        
        # Run Verilator
        compile_result = subprocess.run(
            verilator_cmd,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=60
        )
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            # Try with simpler flags for basic SystemVerilog
            simple_verilator_cmd = [
                "verilator",
                "--cc",
                "--exe",
                "--build",
                "-Wno-fatal",
                str(source_file),
                str(main_file)
            ]
            
            compile_result = subprocess.run(
                simple_verilator_cmd,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                timeout=60
            )
            
            compile_log = compile_result.stdout + compile_result.stderr
            
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "error": "SystemVerilog Compilation Failed",
                    "compile_log": compile_log,
                    "details": {"compile_error": True, "language": "systemverilog"}
                }
        
        # Run the compiled executable
        exec_path = tmp_path / "obj_dir" / "Vdesign"
        if not exec_path.exists():
            exec_path = tmp_path / "Vdesign"
        
        run_result = subprocess.run(
            [str(exec_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        run_log = run_result.stdout + run_result.stderr
        
        # Parse results for assertions and coverage
        assertion_results = []
        coverage_data = {}
        
        if "Assertion" in run_log or "assert" in run_log.lower():
            # Simple assertion parsing
            lines = run_log.split('\n')
            for line in lines:
                if "assert" in line.lower() and "failed" in line.lower():
                    assertion_results.append({
                        "status": "failed",
                        "message": line.strip()
                    })
                elif "assert" in line.lower() and "passed" in line.lower():
                    assertion_results.append({
                        "status": "passed",
                        "message": line.strip()
                    })
        
        if "coverage" in features:
            # Simple coverage reporting
            coverage_data = {
                "line_coverage": "0%",
                "toggle_coverage": "0%",
                "assertion_coverage": "0%"
            }
        
        # Check for success
        success = ("PASS" in run_log or "completed" in run_log or "Simulation completed" in run_log) 
        
        return {
            "success": success,
            "output": run_log,
            "compile_log": compile_log,
            "run_log": run_log,
            "assertion_results": assertion_results,
            "coverage": coverage_data,
            "details": {
                "language": "systemverilog",
                "features_used": features,
                "simulation_completed": True
            }
        }

@app.get("/api/features")
async def get_features():
    """Return available SystemVerilog features"""
    return {
        "languages": ["verilog", "systemverilog"],
        "systemverilog_features": [
            "assertions",
            "constraints",
            "coverage",
            "interfaces",
            "classes",
            "packages",
            "randomization",
            "functional_coverage"
        ],
        "supported_simulators": {
            "verilog": "Icarus Verilog",
            "systemverilog": "Verilator"
        }
    }

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    # Check if tools are available
    tools = {}
    try:
        iverilog_result = subprocess.run(["iverilog", "-V"], capture_output=True, text=True)
        tools["iverilog"] = "available" if iverilog_result.returncode == 0 else "not available"
    except:
        tools["iverilog"] = "not available"
    
    try:
        verilator_result = subprocess.run(["verilator", "--version"], capture_output=True, text=True)
        tools["verilator"] = "available" if verilator_result.returncode == 0 else "not available"
    except:
        tools["verilator"] = "not available"
    
    return {
        "status": "healthy",
        "total_problems": len(ALL_PROBLEMS),
        "verilog_problems": len([p for p in ALL_PROBLEMS if p.get("language", "").lower() != "systemverilog"]),
        "systemverilog_problems": len([p for p in ALL_PROBLEMS if p.get("language", "").lower() == "systemverilog"]),
        "tools": tools
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
