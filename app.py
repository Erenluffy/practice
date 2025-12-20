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
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

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
    language: str = "verilog"

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
            if 'features' in problem and len(problem['features']) > 0:
                problem['language'] = 'systemverilog'
            else:
                problem['language'] = 'verilog'
    
    logger.info(f"Total problems loaded: {len(all_problems)}")
    return all_problems

# Load all problems
ALL_PROBLEMS = load_all_problems()

@app.get("/")
async def root():
    return {
        "status": "VLSI Practice API - SystemVerilog Support", 
        "version": "7.0",
        "verilog_problems": len([p for p in ALL_PROBLEMS if p.get("language") == "verilog"]),
        "systemverilog_problems": len([p for p in ALL_PROBLEMS if p.get("language") == "systemverilog"])
    }

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
            "language": "systemverilog",
            "features": problem.get("features", []),
            "constraints": problem.get("constraints", ""),
            "assertions": problem.get("assertions", ""),
            "expected_output": problem.get("expected_output", "")
        })
    return {"problems": simplified}

@app.get("/api/verilog/problems")
async def get_verilog_problems():
    """Return list of Verilog-only problems"""
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
            raise HTTPException(status_code=404, detail=f"Problem '{request.problem_id}' not found")
        
        # Determine language from problem
        language = problem.get("language", "verilog").lower()
        
        # Log the request
        logger.info(f"Running {language} simulation for problem: {problem['title']}")
        
        # Run simulation
        if language == "systemverilog":
            result = run_systemverilog_simulation_safe(
                request.code,
                problem
            )
        else:
            result = run_verilog_simulation(
                request.code,
                problem.get("testbench", "")
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
            response["constraint_check"] = result.get("constraint_check", {})
        
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
                "error": "Verilog Compilation Failed",
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
        
        # Check for PASS in output
        if "PASS" in run_log.upper():
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

def run_systemverilog_simulation_safe(user_code: str, problem: dict) -> dict:
    """
    Safe SystemVerilog simulation with workarounds for Verilator limitations.
    For constraint problems, we'll do pattern matching instead of full simulation.
    """
    features = problem.get("features", [])
    expected_output = problem.get("expected_output", "")
    
    # For constraint problems, use simplified approach
    if "constraints" in features:
        return check_constraints_pattern(user_code, problem)
    
    # For assertion problems, try Verilator with limited features
    elif "assertions" in features:
        return run_systemverilog_with_assertions(user_code, problem)
    
    # For other SV features, use Icarus Verilog (which has better SV support)
    else:
        return run_systemverilog_with_iverilog(user_code, problem)

def check_constraints_pattern(user_code: str, problem: dict) -> dict:
    """
    Check constraints by analyzing code pattern and running simplified test.
    This avoids Verilator's limitations with classes and randomization.
    """
    # Extract expected pattern from problem
    expected_pattern = problem.get("expected_output", "")
    
    # Check if user code contains required elements
    checks = {
        "has_class": "class" in user_code.lower(),
        "has_rand": "rand" in user_code.lower(),
        "has_constraint": "constraint" in user_code.lower(),
        "has_randomize": "randomize" in user_code or ".randomize()" in user_code,
    }
    
    # Check for specific patterns based on problem
    if "array" in problem.get("description", "").lower():
        checks["has_array"] = "array" in user_code.lower() or "[]" in user_code
        checks["has_sum"] = "sum" in user_code.lower() or ".sum()" in user_code
    
    all_checks_passed = all(checks.values())
    
    # Create a simple test module to verify basic syntax
    test_result = run_simple_sv_test(user_code)
    
    # Analyze results
    if test_result["success"]:
        # Check if output matches expected pattern
        output_matches = False
        if expected_pattern:
            output_lower = test_result["output"].lower()
            pattern_lower = expected_pattern.lower()
            output_matches = pattern_lower in output_lower
        
        success = all_checks_passed and output_matches
        
        return {
            "success": success,
            "output": test_result["output"],
            "error": "" if success else "Constraints not properly implemented",
            "compile_log": test_result["compile_log"],
            "run_log": test_result["run_log"],
            "constraint_check": {
                "satisfied": success,
                "checks": checks,
                "pattern_matched": output_matches,
                "expected_pattern": expected_pattern
            },
            "details": {
                "method": "pattern_check",
                "all_checks_passed": all_checks_passed
            }
        }
    else:
        return {
            "success": False,
            "output": test_result["output"],
            "error": "Syntax error in SystemVerilog code",
            "compile_log": test_result["compile_log"],
            "run_log": test_result["run_log"],
            "constraint_check": {
                "satisfied": False,
                "checks": checks
            },
            "details": {
                "method": "pattern_check",
                "compilation_failed": True
            }
        }

def run_simple_sv_test(user_code: str) -> dict:
    """Run a simplified SystemVerilog test using Icarus Verilog"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create a wrapper that removes unsupported features
        wrapped_code = wrap_sv_code_for_iverilog(user_code)
        
        source_file = tmp_path / "design.sv"
        source_file.write_text(wrapped_code)
        
        # Try to compile with Icarus (has better SV support than Verilator)
        compile_result = subprocess.run(
            ["iverilog", "-g2012", "-o", str(tmp_path / "sim"), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            return {
                "success": False,
                "output": "",
                "compile_log": compile_log,
                "run_log": ""
            }
        
        # Run simulation
        sim_result = subprocess.run(
            ["vvp", str(tmp_path / "sim")],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        run_log = sim_result.stdout + sim_result.stderr
        
        return {
            "success": True,
            "output": run_log,
            "compile_log": compile_log,
            "run_log": run_log
        }

def wrap_sv_code_for_iverilog(user_code: str) -> str:
    """
    Wrap user code to work with Icarus Verilog's limited SV support.
    Removes unsupported class/constraint features for basic syntax checking.
    """
    # Remove class definitions (Icarus doesn't fully support SV classes)
    lines = user_code.split('\n')
    wrapped_lines = []
    in_class = False
    in_constraint = False
    
    for line in lines:
        # Skip class definitions
        if 'class' in line.lower() and 'endclass' not in line.lower():
            in_class = True
            # Replace with simple module for testing
            if 'class' in line and ';' not in line:
                class_name = line.split()[1] if len(line.split()) > 1 else "test_class"
                wrapped_lines.append(f"// Replaced class with module for testing")
                wrapped_lines.append(f"module {class_name};")
                wrapped_lines.append(f"  initial $display(\"Class {class_name} would be here\");")
                wrapped_lines.append(f"endmodule")
            continue
        
        if 'endclass' in line.lower():
            in_class = False
            continue
        
        if 'constraint' in line.lower() and '{' in line:
            in_constraint = True
            # Replace constraint with comment
            wrapped_lines.append(f"  // Constraint: {line.strip()}")
            continue
        
        if in_constraint and '}' in line:
            in_constraint = False
            continue
        
        if not in_class and not in_constraint:
            # Replace .randomize() with simple function call
            if '.randomize()' in line:
                line = line.replace('.randomize()', '_randomize()')
            
            # Replace .sum() with manual sum calculation
            if '.sum()' in line:
                # Simple workaround - replace with fixed value
                line = line.replace('.sum()', '_get_sum()')
            
            wrapped_lines.append(line)
    
    # Add helper functions if needed
    if any('_randomize()' in line for line in wrapped_lines):
        wrapped_lines.append("\nfunction int _randomize();")
        wrapped_lines.append("  return 1; // Always succeed for testing")
        wrapped_lines.append("endfunction")
    
    if any('_get_sum()' in line for line in wrapped_lines):
        wrapped_lines.append("\nfunction int _get_sum();")
        wrapped_lines.append("  return 100; // Fixed sum for testing")
        wrapped_lines.append("endfunction")
    
    # Ensure there's a proper testbench
    if not any('module test' in line.lower() for line in wrapped_lines):
        wrapped_lines.append("\nmodule test;")
        wrapped_lines.append("  initial begin")
        wrapped_lines.append('    $display("Testing SystemVerilog code...");')
        wrapped_lines.append("    #10;")
        wrapped_lines.append('    $display("Basic syntax check passed");')
        wrapped_lines.append("  end")
        wrapped_lines.append("endmodule")
    
    return '\n'.join(wrapped_lines)

def run_systemverilog_with_assertions(user_code: str, problem: dict) -> dict:
    """Run SystemVerilog with assertions using Verilator"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Prepare source - keep it simple
        source = f"`timescale 1ns/1ps\n{user_code}"
        source_file = tmp_path / "design.sv"
        source_file.write_text(source)
        
        # Try Verilator with minimal flags
        verilator_cmd = [
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-Wno-fatal",
            str(source_file),
        ]
        
        # Add main.cpp
        main_cpp = '''#include "Vdesign.h"
#include "verilated.h"
#include <iostream>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vdesign* top = new Vdesign;
    
    // Simple test
    top->eval();
    
    std::cout << "Assertion test completed" << std::endl;
    
    top->final();
    delete top;
    return 0;
}
'''
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
            return {
                "success": False,
                "error": "Assertion compilation failed",
                "compile_log": compile_log[:1000],
                "run_log": "",
                "details": {"compile_error": True}
            }
        
        # Run executable
        exec_path = tmp_path / "obj_dir" / "Vdesign"
        if not exec_path.exists():
            return {
                "success": False,
                "error": "Executable not created",
                "compile_log": compile_log,
                "run_log": "",
                "details": {"executable_missing": True}
            }
        
        run_result = subprocess.run(
            [str(exec_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        run_log = run_result.stdout + run_result.stderr
        
        # Check for assertion messages
        assertion_results = []
        if "assert" in run_log.lower() or "Assertion" in run_log:
            lines = run_log.split('\n')
            for line in lines:
                if "Assertion" in line or "assert" in line.lower():
                    status = "passed" if "passed" in line.lower() else "failed"
                    assertion_results.append({
                        "status": status,
                        "message": line.strip()
                    })
        
        success = not any(r["status"] == "failed" for r in assertion_results)
        
        return {
            "success": success,
            "output": run_log,
            "error": "" if success else "Assertion failures",
            "compile_log": compile_log,
            "run_log": run_log,
            "assertion_results": assertion_results,
            "details": {"method": "verilator_assertions"}
        }

def run_systemverilog_with_iverilog(user_code: str, problem: dict) -> dict:
    """Run SystemVerilog using Icarus Verilog (better SV support than Verilator)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Use Icarus with SystemVerilog support
        source = f"`timescale 1ns/1ps\n{user_code}"
        source_file = tmp_path / "design.sv"
        source_file.write_text(source)
        
        # Compile with Icarus (2012 standard for basic SV support)
        output_exec = tmp_path / "sim"
        compile_result = subprocess.run(
            ["iverilog", "-g2012", "-o", str(output_exec), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            return {
                "success": False,
                "error": "SystemVerilog Compilation Failed",
                "compile_log": compile_log,
                "run_log": "",
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
        
        success = "PASS" in run_log.upper() or "Error" not in run_log
        
        return {
            "success": success,
            "output": run_log,
            "error": "" if success else "Simulation failed",
            "compile_log": compile_log,
            "run_log": run_log,
            "details": {"method": "iverilog_systemverilog"}
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
        "note": "Note: Some advanced SystemVerilog features may have limited support",
        "verilog_simulator": "Icarus Verilog",
        "systemverilog_simulator": "Icarus Verilog (basic) / Verilator (limited)"
    }

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    tools = {}
    try:
        iverilog_result = subprocess.run(["iverilog", "-V"], capture_output=True, text=True)
        tools["iverilog"] = "available" if iverilog_result.returncode == 0 else "not available"
    except:
        tools["iverilog"] = "not available"
    
    try:
        verilator_result = subprocess.run(["verilator", "--version"], capture_output=True, text=True)
        if verilator_result.returncode == 0:
            tools["verilator"] = f"available ({verilator_result.stdout.strip()})"
        else:
            tools["verilator"] = "not available"
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
