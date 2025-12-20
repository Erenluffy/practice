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
            # Auto-detect based on features or file extension in template
            if 'features' in problem and len(problem['features']) > 0:
                problem['language'] = 'systemverilog'
            elif 'systemverilog' in problem.get('template', '').lower() or '.sv' in problem.get('template', ''):
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
        "version": "6.0",
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
            result = run_systemverilog_simulation(
                request.code,
                problem.get("testbench", ""),
                problem.get("constraints", ""),
                problem.get("assertions", ""),
                problem.get("features", []),
                problem.get("expected_output", "")
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
            response["coverage"] = result.get("coverage", {})
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

def run_systemverilog_simulation(user_code: str, testbench: str, constraints: str = "", 
                                assertions: str = "", features: list = None, 
                                expected_output: str = "") -> dict:
    """Run SystemVerilog simulation using Verilator"""
    if features is None:
        features = []
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Prepare SystemVerilog source
        source_parts = []
        
        # Add timescale
        source_parts.append("`timescale 1ns/1ps")
        
        # Add user code
        source_parts.append(user_code)
        
        # Add constraints if provided
        if constraints:
            source_parts.append(constraints)
        
        # Add assertions if provided
        if assertions:
            source_parts.append(assertions)
        
        # Add testbench if provided, otherwise create minimal testbench
        if testbench:
            source_parts.append(testbench)
        else:
            # Create minimal testbench for constraint checking
            source_parts.append(create_minimal_testbench())
        
        # Combine all parts
        source = "\n\n".join(source_parts)
        source_file = tmp_path / "design.sv"
        source_file.write_text(source)
        
        # Prepare Verilator command with proper SystemVerilog support
        verilator_cmd = [
            "verilator",
            "--cc",  # Create C++ output
            "--exe",  # Create executable
            "--build",  # Build the executable
            "-Wno-fatal",  # Don't stop on first error
            "--language", "1800-2017",  # SystemVerilog 2017
            "-Wall",  # Show all warnings
        ]
        
        # Add feature-specific flags
        if "assertions" in features:
            verilator_cmd.append("--assert")
        
        if "coverage" in features:
            verilator_cmd.append("--coverage")
        
        # Add constraint solver flag if constraints are used
        if "constraints" in features and constraints:
            verilator_cmd.append("+define+SYSTEMVERILOG_CONSTRAINTS")
        
        # Add the source file
        verilator_cmd.append(str(source_file))
        
        # Add a better main.cpp for Verilator
        main_cpp = create_verilator_main(features, expected_output)
        main_file = tmp_path / "main.cpp"
        main_file.write_text(main_cpp)
        verilator_cmd.append(str(main_file))
        
        # Run Verilator
        try:
            compile_result = subprocess.run(
                verilator_cmd,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                timeout=60
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Verilator compilation timeout",
                "compile_log": "Compilation took too long (60s timeout)",
                "details": {"timeout": True}
            }
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        if compile_result.returncode != 0:
            # Try simpler approach without language flag
            simple_verilator_cmd = [
                "verilator",
                "--cc",
                "--exe",
                "--build",
                "-Wno-fatal",
                str(source_file),
                str(main_file)
            ]
            
            try:
                compile_result = subprocess.run(
                    simple_verilator_cmd,
                    capture_output=True,
                    text=True,
                    cwd=tmpdir,
                    timeout=60
                )
                compile_log = compile_result.stdout + compile_result.stderr
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": "Verilator compilation timeout (simple mode)",
                    "compile_log": "Compilation took too long",
                    "details": {"timeout": True}
                }
            
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "error": "SystemVerilog Compilation Failed",
                    "compile_log": compile_log[:2000],
                    "details": {"compile_error": True, "language": "systemverilog"}
                }
        
        # Run the compiled executable
        exec_path = tmp_path / "obj_dir" / "Vdesign"
        if not exec_path.exists():
            # Try alternative path
            exec_path = tmp_path / "Vdesign"
            if not exec_path.exists():
                return {
                    "success": False,
                    "error": "Executable not found after compilation",
                    "compile_log": compile_log,
                    "details": {"executable_missing": True}
                }
        
        try:
            run_result = subprocess.run(
                [str(exec_path)],
                capture_output=True,
                text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Simulation timeout",
                "compile_log": compile_log,
                "run_log": "Simulation took too long (30s timeout)",
                "details": {"timeout": True}
            }
        
        run_log = run_result.stdout + run_result.stderr
        
        # Parse results
        assertion_results = []
        coverage_data = {}
        constraint_check = {}
        
        # Check for assertion results
        if "assert" in run_log.lower():
            lines = run_log.split('\n')
            for line in lines:
                line_lower = line.lower()
                if "assertion" in line_lower and "failed" in line_lower:
                    assertion_results.append({
                        "status": "failed",
                        "message": line.strip()
                    })
                elif "assertion" in line_lower and "passed" in line_lower:
                    assertion_results.append({
                        "status": "passed",
                        "message": line.strip()
                    })
        
        # Check for constraint satisfaction
        if "constraints" in features:
            constraint_check = check_constraints_satisfied(run_log)
        
        # Check for expected output if provided
        output_matches = True
        if expected_output:
            # Clean and compare output
            cleaned_output = clean_output(run_log)
            output_matches = expected_output.lower() in cleaned_output.lower()
        
        # Determine success
        success = True
        error_msg = ""
        
        # Check for compilation/simulation errors
        if "error" in run_log.lower() and "assertion" not in run_log.lower():
            success = False
            error_msg = "Simulation error detected"
        
        # Check for failed assertions
        if any(r["status"] == "failed" for r in assertion_results):
            success = False
            error_msg = "Assertion failures detected"
        
        # Check constraint satisfaction
        if constraint_check and not constraint_check.get("satisfied", True):
            success = False
            error_msg = "Constraints not satisfied"
        
        # Check expected output
        if expected_output and not output_matches:
            success = False
            error_msg = "Output doesn't match expected pattern"
        
        # Check for PASS/FAIL in output
        if "FAIL" in run_log.upper() and "PASS" not in run_log.upper():
            success = False
            error_msg = "Test failed (FAIL found in output)"
        
        return {
            "success": success,
            "output": run_log,
            "error": error_msg if not success else "",
            "compile_log": compile_log[:1000],  # Limit compile log size
            "run_log": run_log,
            "assertion_results": assertion_results,
            "coverage": coverage_data,
            "constraint_check": constraint_check,
            "details": {
                "language": "systemverilog",
                "features_used": features,
                "simulation_completed": True,
                "output_matches_expected": output_matches if expected_output else None
            }
        }

def create_minimal_testbench() -> str:
    """Create a minimal testbench for SystemVerilog code"""
    return """
module tb;
    initial begin
        $display("Starting SystemVerilog simulation...");
        
        // Run some cycles
        #10;
        
        // Display completion message
        $display("Simulation completed");
        $finish;
    end
endmodule
"""

def create_verilator_main(features: List[str], expected_output: str = "") -> str:
    """Create C++ main file for Verilator"""
    
    main_cpp = '''#include "Vdesign.h"
#include "verilated.h"
#include <iostream>
#include <string>
#include <regex>

bool check_output_contains(const std::string& output, const std::string& pattern) {
    if (pattern.empty()) return true;
    
    try {
        std::regex re(pattern, std::regex::icase);
        return std::regex_search(output, re);
    } catch (...) {
        // Fallback to simple substring search
        std::string output_lower = output;
        std::string pattern_lower = pattern;
        std::transform(output_lower.begin(), output_lower.end(), output_lower.begin(), ::tolower);
        std::transform(pattern_lower.begin(), pattern_lower.end(), pattern_lower.begin(), ::tolower);
        return output_lower.find(pattern_lower) != std::string::npos;
    }
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    
    // Create instance
    Vdesign* top = new Vdesign;
    
    // Redirect stdout to capture output
    std::stringstream output_stream;
    std::streambuf* old_cout = std::cout.rdbuf(output_stream.rdbuf());
    
    // Initialize
    top->eval();
    
    // Run simulation
    int max_cycles = 1000;
    int cycle = 0;
    
    while (cycle < max_cycles && !Verilated::gotFinish()) {
        top->clk = !top->clk;
        top->eval();
        cycle++;
        
        // Break early if we see finish
        if (Verilated::gotFinish()) {
            break;
        }
    }
    
    // Final evaluation
    top->final();
    
    // Restore cout
    std::cout.rdbuf(old_cout);
    
    // Get output
    std::string output = output_stream.str();
    
    // Always print the output
    std::cout << output;
    
    // Check for expected output pattern
    std::string expected = R"exp(''' + expected_output.replace('"', '\\"') + ''')exp";
    if (!expected.empty() && !check_output_contains(output, expected)) {
        std::cout << "\\nERROR: Output does not contain expected pattern: " << expected << std::endl;
    }
    
    // Check for common errors
    if (output.find("Error") != std::string::npos || 
        output.find("ERROR") != std::string::npos) {
        std::cout << "\\nERROR detected in simulation output" << std::endl;
    }
    
    delete top;
    return 0;
}
'''
    return main_cpp

def check_constraints_satisfied(output: str) -> dict:
    """Check if constraints appear to be satisfied in output"""
    result = {
        "satisfied": True,
        "checks": []
    }
    
    # Look for constraint-related messages
    lines = output.split('\n')
    for line in lines:
        line_lower = line.lower()
        if "constraint" in line_lower:
            if "failed" in line_lower or "violat" in line_lower:
                result["satisfied"] = False
                result["checks"].append({
                    "message": line.strip(),
                    "status": "failed"
                })
            elif "satisfied" in line_lower or "passed" in line_lower:
                result["checks"].append({
                    "message": line.strip(),
                    "status": "passed"
                })
    
    return result

def clean_output(output: str) -> str:
    """Clean simulation output by removing timestamps and other noise"""
    lines = output.split('\n')
    cleaned_lines = []
    
    for line in lines:
        # Remove lines with just numbers (timestamps)
        if re.match(r'^\s*\d+\s*$', line):
            continue
        # Remove VCD dump messages
        if 'VCD' in line and 'dumpfile' in line:
            continue
        # Remove empty lines
        if line.strip() == '':
            continue
        cleaned_lines.append(line.strip())
    
    return '\n'.join(cleaned_lines)

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
