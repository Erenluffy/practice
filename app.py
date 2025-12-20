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
        "version": "8.0",
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
            result = validate_systemverilog_code(
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
            response["validation_results"] = result.get("validation_results", {})
        
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
        if "PASS" in run_log.upper() and "FAIL" not in run_log.upper():
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

def validate_systemverilog_code(user_code: str, problem: dict) -> dict:
    """
    Validate SystemVerilog code through pattern matching and syntax checking.
    For constraint problems, we validate the structure without running full simulation.
    """
    features = problem.get("features", [])
    problem_id = problem.get("id", "")
    
    # First, do basic syntax check with a simple wrapper
    syntax_result = check_sv_syntax(user_code)
    
    if not syntax_result["success"]:
        return {
            "success": False,
            "output": "",
            "error": "SystemVerilog syntax error",
            "compile_log": syntax_result["compile_log"],
            "run_log": "",
            "validation_results": {
                "syntax_check": False,
                "error": syntax_result["error"]
            },
            "details": {"syntax_error": True}
        }
    
    # Then validate based on problem type
    validation_results = {}
    
    if "constraints" in features:
        validation_results = validate_constraint_code(user_code, problem)
    elif "assertions" in features:
        validation_results = validate_assertion_code(user_code, problem)
    elif "coverage" in features:
        validation_results = validate_coverage_code(user_code, problem)
    else:
        # Generic SV validation
        validation_results = validate_generic_sv(user_code, problem)
    
    # Determine overall success
    all_passed = all(validation_results.get("checks", {}).values())
    
    if all_passed:
        return {
            "success": True,
            "output": f"✅ SystemVerilog code validation passed!\n\n" +
                     f"Problem: {problem['title']}\n" +
                     f"Checks passed: {len([v for v in validation_results.get('checks', {}).values() if v])}/{len(validation_results.get('checks', {}))}\n" +
                     (f"Matched pattern: {validation_results.get('pattern_matched', 'N/A')}\n" if 'pattern_matched' in validation_results else ""),
            "error": "",
            "compile_log": syntax_result["compile_log"],
            "run_log": syntax_result["output"],
            "validation_results": validation_results,
            "details": {
                "validation_method": "pattern_matching",
                "all_checks_passed": True
            }
        }
    else:
        return {
            "success": False,
            "output": f"❌ SystemVerilog code validation failed\n\n" +
                     f"Problem: {problem['title']}\n" +
                     f"Checks passed: {len([v for v in validation_results.get('checks', {}).values() if v])}/{len(validation_results.get('checks', {}))}\n" +
                     "Failed checks:\n" +
                     "\n".join([f"  • {k}: {v}" for k, v in validation_results.get('checks', {}).items() if not v]),
            "error": "Code doesn't meet requirements",
            "compile_log": syntax_result["compile_log"],
            "run_log": syntax_result["output"],
            "validation_results": validation_results,
            "details": {
                "validation_method": "pattern_matching",
                "all_checks_passed": False
            }
        }

def check_sv_syntax(user_code: str) -> dict:
    """
    Check if the code has valid SystemVerilog syntax using Icarus.
    We'll wrap it in a simple testbench to avoid compilation errors.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create a simple wrapper for syntax checking
        wrapped_code = create_syntax_check_wrapper(user_code)
        
        source_file = tmp_path / "design.sv"
        source_file.write_text(wrapped_code)
        
        # Try to compile with Icarus
        compile_result = subprocess.run(
            ["iverilog", "-g2012", "-o", str(tmp_path / "sim"), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            # Try without -g2012 flag
            compile_result = subprocess.run(
                ["iverilog", "-o", str(tmp_path / "sim"), str(source_file)],
                capture_output=True,
                text=True,
                timeout=30
            )
            compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            return {
                "success": False,
                "error": "Syntax error",
                "compile_log": compile_log,
                "output": ""
            }
        
        # Try to run it
        sim_result = subprocess.run(
            ["vvp", str(tmp_path / "sim")],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = sim_result.stdout + sim_result.stderr
        
        return {
            "success": True,
            "error": "",
            "compile_log": compile_log,
            "output": output
        }

def create_syntax_check_wrapper(user_code: str) -> str:
    """
    Create a wrapper for syntax checking that handles various SV constructs.
    """
    lines = user_code.split('\n')
    wrapped_lines = []
    
    # Remove problematic lines but keep structure
    for line in lines:
        # Skip unsupported constructs but keep them for pattern matching
        wrapped_lines.append(line)
    
    # Ensure there's a proper testbench at the end
    if not any('module test' in line.lower() for line in wrapped_lines) and \
       not any('initial begin' in line.lower() for line in wrapped_lines):
        wrapped_lines.append("\nmodule syntax_test;")
        wrapped_lines.append("  initial begin")
        wrapped_lines.append('    $display("Syntax check passed");')
        wrapped_lines.append("    #10;")
        wrapped_lines.append("  end")
        wrapped_lines.append("endmodule")
    
    return '\n'.join(wrapped_lines)

def validate_constraint_code(user_code: str, problem: dict) -> dict:
    """Validate constraint-based SystemVerilog code"""
    checks = {}
    
    # Basic checks
    checks["has_class"] = "class" in user_code.lower()
    checks["has_rand"] = "rand" in user_code.lower()
    checks["has_constraint"] = "constraint" in user_code.lower()
    
    # Problem-specific checks based on description
    description = problem.get("description", "").lower()
    title = problem.get("title", "").lower()
    
    if "array" in description or "array" in title:
        checks["has_array"] = "array" in user_code.lower() or "[]" in user_code
        checks["has_size_check"] = "size" in user_code.lower() and ("< 10" in user_code or "<10" in user_code)
        checks["has_sum_check"] = "sum" in user_code.lower() and ("== 100" in user_code or "==100" in user_code)
    
    if "fifo" in description or "fifo" in title:
        checks["has_fifo_logic"] = any(word in user_code.lower() for word in ["wr_en", "rd_en", "full", "empty"])
    
    # Check for proper constraint syntax
    constraint_patterns = [
        r"constraint\s+\w+\s*{",
        r"rand\s+(int|bit|logic)\s+",
        r"randomize\s*\(\s*\)"
    ]
    
    constraint_matches = 0
    for pattern in constraint_patterns:
        if re.search(pattern, user_code, re.IGNORECASE):
            constraint_matches += 1
    
    checks["proper_constraint_syntax"] = constraint_matches >= 2
    
    # Check for display/output
    checks["has_display"] = "\$display" in user_code or "\$write" in user_code
    
    # Check for test module
    checks["has_test_module"] = "module test" in user_code.lower() or "initial begin" in user_code.lower()
    
    return {
        "checks": checks,
        "pattern_matched": True,
        "validation_method": "pattern_matching"
    }

def validate_assertion_code(user_code: str, problem: dict) -> dict:
    """Validate assertion-based SystemVerilog code"""
    checks = {}
    
    # Basic assertion checks
    checks["has_assert"] = "assert" in user_code.lower()
    checks["has_property"] = "property" in user_code.lower() or "@(posedge" in user_code
    
    # Check for common assertion patterns
    assertion_patterns = [
        r"assert\s+property",
        r"@\(posedge",
        r"disable\s+iff",
        r"\$error",
        r"!\$isunknown"
    ]
    
    assertion_matches = 0
    for pattern in assertion_patterns:
        if re.search(pattern, user_code, re.IGNORECASE):
            assertion_matches += 1
    
    checks["proper_assertion_syntax"] = assertion_matches >= 2
    
    # Check for testbench
    checks["has_testbench"] = "module" in user_code and "initial" in user_code
    
    return {
        "checks": checks,
        "validation_method": "assertion_patterns"
    }

def validate_coverage_code(user_code: str, problem: dict) -> dict:
    """Validate coverage-based SystemVerilog code"""
    checks = {}
    
    # Coverage specific checks
    checks["has_covergroup"] = "covergroup" in user_code.lower()
    checks["has_coverpoint"] = "coverpoint" in user_code.lower()
    checks["has_bins"] = "bins" in user_code.lower()
    
    # Check for sampling
    checks["has_sample"] = "sample" in user_code.lower() or "@(" in user_code
    
    return {
        "checks": checks,
        "validation_method": "coverage_patterns"
    }

def validate_generic_sv(user_code: str, problem: dict) -> dict:
    """Validate generic SystemVerilog code"""
    checks = {}
    
    # Basic SV constructs
    checks["has_module"] = "module" in user_code
    checks["has_initial_or_always"] = "initial" in user_code or "always" in user_code
    checks["has_system_tasks"] = any(task in user_code for task in ["$display", "$write", "$finish"])
    
    # Check for problem-specific requirements
    expected_output = problem.get("expected_output", "")
    if expected_output:
        # Simple pattern matching in code (not output)
        checks["has_expected_pattern"] = expected_output.lower() in user_code.lower()
    
    return {
        "checks": checks,
        "validation_method": "generic_patterns"
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
        "note": "SystemVerilog constraint validation uses pattern matching (not full simulation)",
        "verilog_simulator": "Icarus Verilog (full simulation)",
        "systemverilog_simulator": "Icarus Verilog (syntax check) + Pattern matching"
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
