#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Verilator Only
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

app = FastAPI(title="VLSI Practice API - Verilator Only")

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

# Load problems
PROBLEMS = []
if os.path.exists("sv_problems.json"):
    with open("sv_problems.json", "r") as f:
        PROBLEMS = json.load(f)
    logger.info(f"Loaded {len(PROBLEMS)} SystemVerilog problems")

@app.get("/")
async def root():
    return {
        "status": "VLSI Practice API - Verilator Only", 
        "version": "9.0",
        "problems": len(PROBLEMS)
    }

@app.get("/api/problems")
async def get_all_problems():
    """Return all problems"""
    simplified = []
    for problem in PROBLEMS:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"],
            "language": "systemverilog",
            "features": problem.get("features", [])
        })
    return {"problems": simplified}

@app.post("/api/run")
async def run_code(request: CodeRequest):
    """Execute SystemVerilog code using Verilator"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail=f"Problem '{request.problem_id}' not found")
        
        logger.info(f"Running SystemVerilog simulation for problem: {problem['title']}")
        
        # Run Verilator simulation
        result = run_verilator_simulation(
            request.code,
            problem
        )
        
        # Prepare response
        response = {
            "success": result["success"],
            "problem": problem["title"],
            "language": "systemverilog",
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", {}),
            "compile_log": result.get("compile_log", ""),
            "run_log": result.get("run_log", ""),
            "validation": result.get("validation", {})
        }
        
        if not result["success"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def run_verilator_simulation(user_code: str, problem: dict) -> dict:
    """Run SystemVerilog code through Verilator"""
    
    # First, validate the code structure
    validation = validate_code_structure(user_code, problem)
    
    # Create a testbench if needed
    testbench = create_verilator_testbench(user_code, problem)
    
    # Combine code and testbench
    full_code = f"{user_code}\n\n{testbench}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Write the SystemVerilog file
        sv_file = tmp_path / "design.sv"
        sv_file.write_text(full_code)
        
        # Prepare Verilator command
        verilator_cmd = [
            "verilator",
            "--cc",  # Create C++ output
            "--exe",  # Create executable
            "--build",  # Build the executable
            "-Wno-fatal",  # Don't stop on first error
            "--language", "1800-2017",  # SystemVerilog 2017
            "-Wall",  # Show all warnings
            str(sv_file)
        ]
        
        # Add main.cpp
        main_cpp = create_simple_main_cpp()
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
                "validation": validation,
                "details": {"timeout": True}
            }
        
        compile_log = compile_result.stdout + compile_result.stderr
        
        # Check if compilation succeeded
        if compile_result.returncode != 0:
            # Try without language flag
            simple_cmd = [
                "verilator",
                "--cc",
                "--exe",
                "--build",
                "-Wno-fatal",
                str(sv_file),
                str(main_file)
            ]
            
            try:
                compile_result = subprocess.run(
                    simple_cmd,
                    capture_output=True,
                    text=True,
                    cwd=tmpdir,
                    timeout=60
                )
                compile_log = compile_result.stdout + compile_result.stderr
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": "Verilator compilation timeout (simple)",
                    "compile_log": compile_log,
                    "validation": validation,
                    "details": {"timeout": True}
                }
            
            if compile_result.returncode != 0:
                # Extract error message
                error_msg = "Verilator compilation failed"
                error_lines = []
                for line in compile_log.split('\n'):
                    if '%Error' in line or 'error:' in line.lower():
                        error_lines.append(line)
                        if len(error_lines) >= 3:
                            break
                
                if error_lines:
                    error_msg += ":\n" + "\n".join(error_lines[:3])
                
                return {
                    "success": False,
                    "error": error_msg,
                    "compile_log": compile_log[:2000],
                    "validation": validation,
                    "details": {"compile_error": True}
                }
        
        # Run the compiled executable
        exec_path = tmp_path / "obj_dir" / "Vdesign"
        if not exec_path.exists():
            return {
                "success": False,
                "error": "Executable not created",
                "compile_log": compile_log,
                "validation": validation,
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
                "validation": validation,
                "details": {"timeout": True}
            }
        
        run_log = run_result.stdout + run_result.stderr
        
        # Analyze results
        success = determine_success(run_log, validation, problem)
        
        return {
            "success": success,
            "output": run_log,
            "error": "" if success else "Simulation failed or validation errors",
            "compile_log": compile_log[:1000],
            "run_log": run_log,
            "validation": validation,
            "details": {
                "method": "verilator",
                "simulation_ran": True
            }
        }

def validate_code_structure(user_code: str, problem: dict) -> dict:
    """Validate code structure without compiling"""
    checks = {}
    features = problem.get("features", [])
    
    # Basic checks
    checks["has_module"] = "module" in user_code
    checks["has_initial"] = "initial" in user_code or "always" in user_code
    
    # Feature-specific checks
    if "constraints" in features:
        checks["has_class"] = "class" in user_code
        checks["has_rand"] = "rand" in user_code
        checks["has_constraint"] = "constraint" in user_code
        
    if "assertions" in features:
        checks["has_assert"] = "assert" in user_code
        checks["has_property"] = "property" in user_code or "@(posedge" in user_code
        
    if "coverage" in features:
        checks["has_covergroup"] = "covergroup" in user_code
        checks["has_coverpoint"] = "coverpoint" in user_code
    
    # Check for SystemVerilog specific constructs
    sv_keywords = ["logic", "always_ff", "always_comb", "typedef", "interface", "package"]
    checks["has_sv_keywords"] = any(keyword in user_code for keyword in sv_keywords)
    
    # Check for $display (output)
    checks["has_display"] = "$display" in user_code or "$write" in user_code
    
    # Check for problem-specific requirements
    description = problem.get("description", "").lower()
    if "array" in description:
        checks["has_array"] = "array" in user_code or "[]" in user_code
        checks["has_size"] = "size" in user_code
        checks["has_sum"] = "sum" in user_code
    
    if "fifo" in description:
        checks["has_fifo"] = any(word in user_code for word in ["fifo", "wr_en", "rd_en", "full", "empty"])
    
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "score": f"{sum(checks.values())}/{len(checks)}"
    }

def create_verilator_testbench(user_code: str, problem: dict) -> str:
    """Create a testbench suitable for Verilator"""
    
    # Check if user already has a testbench
    if "module test" in user_code.lower() or "module tb" in user_code.lower():
        return ""
    
    # Extract module name if possible
    module_match = re.search(r'module\s+(\w+)', user_code)
    module_name = module_match.group(1) if module_match else "design"
    
    # Create a simple testbench
    testbench = f"""
// Testbench for {problem['title']}
module tb;
    // Clock and reset
    logic clk = 0;
    logic rst_n = 0;
    
    // Create instance
    {module_name} dut ();
    
    // Clock generation
    always #5 clk = ~clk;
    
    // Test sequence
    initial begin
        $display("Starting test for: {problem['title']}");
        
        // Reset
        rst_n = 0;
        #20;
        rst_n = 1;
        #10;
        
        // Basic test
        $display("Running basic test...");
        
        // Add some test cycles
        repeat(10) @(posedge clk);
        
        $display("Test completed");
        $finish;
    end
    
    // Monitor
    initial begin
        $monitor($time, " clk=%b rst_n=%b", clk, rst_n);
    end
endmodule
"""
    
    return testbench

def create_simple_main_cpp() -> str:
    """Create a simple main.cpp for Verilator"""
    return '''#include "Vdesign.h"
#include "verilated.h"
#include <iostream>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    
    // Create instance
    Vdesign* top = new Vdesign;
    
    // Initialize
    top->eval();
    
    // Simple simulation loop
    for (int i = 0; i < 100; i++) {
        // Toggle clock if port exists
        #ifdef TOP_CLK
        top->clk = !top->clk;
        #endif
        
        top->eval();
        
        // Check for finish
        if (Verilated::gotFinish()) {
            std::cout << "Simulation finished early" << std::endl;
            break;
        }
    }
    
    // Cleanup
    top->final();
    delete top;
    
    std::cout << "Simulation completed successfully" << std::endl;
    return 0;
}
'''

def determine_success(run_log: str, validation: dict, problem: dict) -> bool:
    """Determine if simulation was successful"""
    
    # Check for errors in output
    if "ERROR" in run_log.upper() or "error:" in run_log.lower():
        return False
    
    # Check for assertion failures
    if "assert" in run_log.lower() and "failed" in run_log.lower():
        return False
    
    # Check validation passed
    if not validation.get("passed", False):
        return False
    
    # Check for expected output pattern
    expected_output = problem.get("expected_output", "")
    if expected_output:
        if expected_output.lower() not in run_log.lower():
            return False
    
    # If we got here and simulation completed, it's a success
    if "Simulation completed" in run_log or "Test completed" in run_log:
        return True
    
    # Default to true if no errors
    return "Error" not in run_log

@app.get("/api/features")
async def get_features():
    """Return available features"""
    return {
        "simulator": "Verilator 5.0+",
        "language": "SystemVerilog 2017",
        "supported_features": [
            "Modules",
            "Interfaces",
            "Packages",
            "Classes (basic)",
            "Assertions (SVA)",
            "Coverage (basic)",
            "Constraints (syntax only)",
            "Randomization (syntax only)"
        ],
        "note": "Advanced SystemVerilog features may have limited simulation support"
    }

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    tools = {}
    try:
        verilator_result = subprocess.run(["verilator", "--version"], capture_output=True, text=True)
        if verilator_result.returncode == 0:
            version = verilator_result.stdout.strip()
            tools["verilator"] = f"available ({version})"
        else:
            tools["verilator"] = "not available"
    except:
        tools["verilator"] = "not available"
    
    return {
        "status": "healthy" if tools.get("verilator", "").startswith("available") else "degraded",
        "problems": len(PROBLEMS),
        "tools": tools
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
