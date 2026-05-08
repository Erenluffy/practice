#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Fixed with Test Pass Detection
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import subprocess
import tempfile
import os
import sys
import json
import uuid
import shutil
import logging
import re
import asyncio
import time
from threading import Thread
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limiter (per IP)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="VLSI Practice API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Concurrency semaphore — max 3 simulations running at once
MAX_CONCURRENT_SIMS = 3
sim_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SIMS)

# Create waveform directory
WAVEFORM_DIR = Path("/tmp/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"Waveform directory: {WAVEFORM_DIR}")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Background waveform cleanup — delete VCDs older than 1 hour
def _cleanup_waveforms():
    while True:
        try:
            time.sleep(3600)
            now = time.time()
            for f in WAVEFORM_DIR.glob("*.vcd"):
                try:
                    if now - f.stat().st_mtime > 3600:
                        f.unlink()
                        logger.info(f"Cleaned up old waveform: {f.name}")
                except Exception as e:
                    logger.warning(f"Failed to delete {f.name}: {e}")
        except Exception as e:
            logger.error(f"Waveform cleanup error: {e}")

Thread(target=_cleanup_waveforms, daemon=True).start()

# Models
class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"
    generate_waveform: bool = False
    
class SubmitRequest(BaseModel):
    problem_id: str
    code: str
    user_id: str = "anonymous"

# Add this function right before loading PROBLEMS
def clean_json(text):
    """Remove control characters that break JSON parsing"""
    return ''.join(char for char in text if ord(char) >= 32 or char in '\n\r\t')

# Load problems
PROBLEMS = []
try:
    with open("problems.json", "r", encoding="utf-8") as f:
        content = f.read()
        cleaned_content = clean_json(content)  # Clean it!
        PROBLEMS = json.loads(cleaned_content)
    logger.info(f"Loaded {len(PROBLEMS)} problems")
except Exception as e:
    logger.error(f"Error loading problems: {e}")
    PROBLEMS = []

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "5.0", "features": ["test-pass-detection", "manual-submit"]}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    simplified = []
    for problem in PROBLEMS:
        simplified.append({
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"],
            "hint": problem.get("hint", ""),
            'examples': problem.get('examples', []),
            'constraints': problem.get('constraints', []),
            'test_cases': problem.get('test_cases', []),
        })
    return {"problems": simplified}

@app.get("/api/waveform/{waveform_id}/data")
async def get_waveform_data(waveform_id: str):
    """Return parsed VCD waveform data as JSON for frontend viewer"""
    try:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        if not vcd_path.exists():
            raise HTTPException(status_code=404, detail="Waveform not found")
        parser = VCDParser(vcd_path)
        if not parser.parse():
            raise HTTPException(status_code=500, detail="VCD parse failed")
        colors = ['#FF5252','#4CAF50','#2196F3','#FF9800','#9C27B0',
                  '#00BCD4','#8BC34A','#FF5722','#607D8B','#795548']
        signals = parser.signals[:30]
        for i, sig in enumerate(signals):
            sig['color'] = colors[i % len(colors)]
        waveform = {sig['name']: parser.waveform_data[sig['name']] for sig in signals}
        return {
            "signals": signals,
            "waveform": waveform,
            "timescale": parser.timescale,
            "max_time": parser.max_time
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str, download: bool = False):
    """Serve waveform with professional HTML viewer"""
    try:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        if download and vcd_path.exists():
            return FileResponse(
                vcd_path,
                media_type="application/octet-stream",
                filename=f"{waveform_id}.vcd"
            )
        
        # Return professional HTML viewer
        html_content = create_professional_viewer(waveform_id, vcd_path.exists())
        return HTMLResponse(content=html_content)
            
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run")
@limiter.limit("10/minute")
async def run_code(request: Request, body: CodeRequest):
    """Execute Verilog code - Simulation only"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == body.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Semaphore: max MAX_CONCURRENT_SIMS simulations at once, 15s queue timeout
        try:
            await asyncio.wait_for(sim_semaphore.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail="Server is busy. Please try again in a moment."
            )
        
        try:
            result = run_simulation(
                body.code,
                problem["testbench"],
                body.generate_waveform,
                problem["title"]
            )
        finally:
            sim_semaphore.release()
        
        # Prepare response
        response = {
            "success": result["success"],
            "problem": problem["title"],
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", ""),
            "passed": result.get("passed", False)
        }
        
        if not result["success"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        # Add waveform info
        if "waveform_id" in result:
            waveform_id = result["waveform_id"]
            response["waveform_id"] = waveform_id
            response["waveform_url"] = f"/api/waveform/{waveform_id}"
            response["waveform_download_url"] = f"/api/waveform/{waveform_id}?download=true"
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in run_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/submit")
@limiter.limit("10/minute")
async def submit_solution(request: Request, body: SubmitRequest):
    """Submit solution and check if it's correct"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == body.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Semaphore: max MAX_CONCURRENT_SIMS simulations at once, 15s queue timeout
        try:
            await asyncio.wait_for(sim_semaphore.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail="Server is busy. Please try again in a moment."
            )
        
        try:
            result = run_simulation(
                body.code,
                problem["testbench"],
                generate_waveform=False,
                problem_title=problem["title"],
                is_submission=True
            )
        finally:
            sim_semaphore.release()
        
        # Prepare response
        response = {
            "success": result["success"],
            "passed": result.get("passed", False),
            "problem": problem["title"],
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", ""),
            "message": result.get("message", "")
        }
        
        # If failed, add hint
        if not result["passed"] and "hint" in problem:
            response["hint"] = problem["hint"]
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in submit_solution: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _set_resource_limits():
    """Set CPU + memory limits on the child process (Linux only)"""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))          # 10s CPU time
        resource.setrlimit(resource.RLIMIT_AS,  (256 * 1024 * 1024, 256 * 1024 * 1024))  # 256MB RAM
    except Exception:
        pass  # Windows or unsupported platform — skip silently


def run_simulation(user_code: str, testbench: str, generate_waveform: bool, problem_title: str, is_submission: bool = False) -> dict:
    """Run Verilog simulation with improved pass detection"""
    waveform_id = None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Inject VCD dump so waveform viewer works
        if generate_waveform:
            waveform_id = str(uuid.uuid4())
            vcd_path = str(tmp_path / "waveform.vcd").replace("\\", "/")
            if "$dumpfile" in testbench:
                # Testbench already calls $dumpfile — just rewrite the path to our temp dir
                testbench = re.sub(
                    r'\$dumpfile\s*\(\s*"[^"]*"\s*\)',
                    f'$dumpfile("{vcd_path}")',
                    testbench
                )
            else:
                # Inject dump block INSIDE the last module, just before its endmodule
                tb_mod_match = re.findall(r'^\s*module\s+(\w+)', testbench, re.MULTILINE)
                tb_mod = tb_mod_match[-1] if tb_mod_match else None
                dumpvars_line = f'    $dumpvars(0, {tb_mod});\n' if tb_mod else '    $dumpvars(0);\n'
                dump_block = (
                    f'\ninitial begin\n'
                    f'    $dumpfile("{vcd_path}");\n'
                    f'{dumpvars_line}'
                    f'end\n'
                )
                # Insert before the last endmodule in testbench
                last_end = testbench.rfind('endmodule')
                if last_end != -1:
                    testbench = testbench[:last_end] + dump_block + testbench[last_end:]
                else:
                    testbench = testbench + dump_block

        # Strip timescale from both user_code and testbench — we inject one canonical
        # `timescale 1ns/1ps at the top to avoid "duplicate timescale" / port-decl errors
        timescale_re = re.compile(r'`timescale\s+\S+/\S+[ \t]*\n?')
        user_code_clean = timescale_re.sub('', user_code)
        testbench_clean = timescale_re.sub('', testbench)

        # Combine source
        source = f"`timescale 1ns/1ps\n{user_code_clean}\n{testbench_clean}"
        source_file = tmp_path / "design.v"
        source_file.write_text(source)

        # Compile
        output_exec = tmp_path / "sim"
        compile_result = subprocess.run(
            ["iverilog", "-g2012", "-o", str(output_exec), str(source_file)],
            capture_output=True,
            text=True,
            timeout=30
        )

        if compile_result.returncode != 0:
            return {
                "success": False,
                "passed": False,
                "error": "Compilation Failed",
                "details": compile_result.stderr[:500]
            }

        # Simulate with resource limits (Linux) and hard 20s wall-clock timeout
        try:
            sim_result = subprocess.run(
                ["vvp", str(output_exec)],
                capture_output=True,
                text=True,
                timeout=20,
                preexec_fn=_set_resource_limits if sys.platform != "win32" else None
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "passed": False,
                "error": "Simulation Timeout",
                "details": "Simulation exceeded 20 seconds. Check for infinite loops."
            }

        # FIX: Check vvp exit code before trusting output
        if sim_result.returncode not in (0, 1):  # vvp uses 1 for normal $finish
            return {
                "success": False,
                "passed": False,
                "error": "Simulation Runtime Error",
                "details": (sim_result.stderr or sim_result.stdout)[:500]
            }

        output = sim_result.stdout + sim_result.stderr

        # Pass/fail detection
        passed = False
        message = ""

        if "PASS" in output.upper():
            passed = True
            message = "All tests passed!"
        elif "FAIL" in output.upper():
            passed = False
            message = "Tests failed"
        elif "ERROR" in output.upper():
            passed = False
            message = "Runtime error"
        elif "SIMULATION FINISHED" in output.upper():
            if "assertion" in output.lower() and "failed" in output.lower():
                passed = False
                message = "Assertions failed"
            elif "$finish" in output:
                error_lines = [line for line in output.split('\n') if 'error' in line.lower()]
                if error_lines:
                    passed = False
                    message = f"Errors found: {error_lines[0][:100]}"
                else:
                    passed = True
                    message = "Simulation completed successfully"
            else:
                passed = True
                message = "Simulation completed"
        else:
            passed = True
            message = "Code executed successfully (manual verification recommended)"

        result = {
            "success": True,
            "passed": passed,
            "output": output[:2000],
            "message": message
        }

        # Save waveform — only if file exists and has content
        if generate_waveform and waveform_id:
            vcd_file = tmp_path / "waveform.vcd"
            if vcd_file.exists() and vcd_file.stat().st_size > 0:
                dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                shutil.copy2(vcd_file, dest_vcd)
                logger.info(f"Waveform saved: {waveform_id} ({vcd_file.stat().st_size} bytes)")
                result["waveform_id"] = waveform_id
            else:
                logger.warning(f"VCD file missing or empty for {problem_title}")

        return result

class VCDParser:
    """Parse VCD files and extract waveform data"""
    
    def __init__(self, vcd_path):
        self.vcd_path = vcd_path
        self.signals = []
        self.waveform_data = {}
        self.timescale = "1ns"
        self.max_time = 0
        
    def parse(self):
        """Parse the VCD file"""
        try:
            with open(self.vcd_path, 'r') as f:
                content = f.read()
            
            lines = content.split('\n')
            
            # Parse header
            signal_map = {}
            in_var_scope = False
            current_scope = ""
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                    
                # Parse timescale
                if line.startswith('$timescale'):
                    parts = line.split()
                    if len(parts) > 1:
                        self.timescale = parts[1]
                
                # Parse scope
                elif line.startswith('$scope'):
                    parts = line.split()
                    if len(parts) >= 3:
                        current_scope = parts[2]
                        in_var_scope = True
                
                # Parse variable definitions
                elif line.startswith('$var'):
                    parts = line.split()
                    if len(parts) >= 5:
                        var_type = parts[1]
                        width = parts[2]
                        var_id = parts[3]
                        var_name = parts[4]
                        
                        # Clean up var_name (remove $end if present)
                        if var_name.endswith('$end'):
                            var_name = var_name[:-4].strip()
                        
                        # Create full hierarchical name
                        full_name = f"{current_scope}.{var_name}" if current_scope else var_name
                        
                        signal_map[var_id] = full_name
                        self.signals.append({
                            'id': var_id,
                            'name': full_name,
                            'short_name': var_name,
                            'type': var_type,
                            'width': width,
                            'scope': current_scope
                        })
                
                # End of scope
                elif line.startswith('$upscope'):
                    current_scope = ""
                    in_var_scope = False
                
                # End of definitions
                elif line.startswith('$enddefinitions'):
                    break
            
            # Initialize waveform data
            for signal in self.signals:
                self.waveform_data[signal['name']] = []
            
            # Parse value changes
            current_time = 0
            signal_values = {sig['id']: 'x' for sig in self.signals}
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Time change
                if line.startswith('#'):
                    try:
                        time_val = int(line[1:])
                        if time_val != current_time:
                            # Record state at time change
                            self._record_state(current_time, signal_values)
                            current_time = time_val
                            if current_time > self.max_time:
                                self.max_time = current_time
                    except ValueError:
                        continue
                
                # Scalar value change
                elif line[0] in ['0', '1', 'x', 'z', 'X', 'Z'] and len(line) > 1:
                    value = line[0].lower()
                    var_id = line[1:]
                    if var_id in signal_values:
                        signal_values[var_id] = value
                
                # Vector value change
                elif line[0] in ['b', 'B']:
                    parts = line[1:].split()
                    if len(parts) >= 2:
                        value = parts[0]
                        var_id = parts[1]
                        if var_id in signal_values:
                            signal_values[var_id] = value
            
            # Record final state
            self._record_state(current_time, signal_values)
            
            # Clean up signals (remove empty ones)
            self.signals = [sig for sig in self.signals if len(self.waveform_data[sig['name']]) > 0]
            
            return True
            
        except Exception as e:
            logger.error(f"VCD parsing error: {e}")
            return False
    
    def _record_state(self, time, signal_values):
        """Record signal states at a specific time"""
        for sig_id, value in signal_values.items():
            signal_name = None
            for sig in self.signals:
                if sig['id'] == sig_id:
                    signal_name = sig['name']
                    break
            
            if signal_name:
                waveform = self.waveform_data[signal_name]
                if not waveform or waveform[-1]['time'] != time:
                    waveform.append({
                        'time': time,
                        'value': value
                    })
    
    def get_waveform_summary(self, signal_name=None):
        """Get summary of waveform data"""
        if signal_name:
            return self.waveform_data.get(signal_name, [])
        
        summary = {}
        for sig in self.signals[:10]:  # Limit to 10 signals for performance
            summary[sig['name']] = self.waveform_data[sig['name']]
        return summary

def create_professional_viewer(waveform_id: str, vcd_exists: bool) -> str:
    """Create professional HTML viewer with actual waveform display"""
    
    # Parse VCD file
    signals_data = []
    waveform_summary = {}
    timescale = "1ns"
    max_time = 100
    
    if vcd_exists:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        if vcd_path.exists():
            try:
                parser = VCDParser(vcd_path)
                if parser.parse():
                    signals_data = parser.signals[:20]  # Limit to 20 signals
                    waveform_summary = parser.get_waveform_summary()
                    timescale = parser.timescale
                    max_time = parser.max_time
                    
                    # Add colors to signals
                    colors = ['#FF5252', '#4CAF50', '#2196F3', '#FF9800', '#9C27B0', 
                             '#00BCD4', '#8BC34A', '#FF5722', '#607D8B', '#795548']
                    for i, sig in enumerate(signals_data):
                        sig['color'] = colors[i % len(colors)]
            except Exception as e:
                logger.error(f"Error parsing VCD: {e}")
    
    # If no signals found, create sample data for demo
    if not signals_data:
        signals_data = [
            {'id': '1', 'name': 'clk', 'short_name': 'clk', 'color': '#FF5252', 'width': '1'},
            {'id': '2', 'name': 'a', 'short_name': 'a', 'color': '#4CAF50', 'width': '1'},
            {'id': '3', 'name': 'b', 'short_name': 'b', 'color': '#2196F3', 'width': '1'},
            {'id': '4', 'name': 'out', 'short_name': 'out', 'color': '#FF9800', 'width': '1'},
        ]
        timescale = "1ns"
        max_time = 100
    
    # Prepare data for JavaScript
    signals_json = json.dumps(signals_data)
    timescale_json = json.dumps(timescale)
    max_time_json = json.dumps(max_time)
    
    # Create a simplified waveform data structure for JavaScript
    sample_waveform = {}
    for sig in signals_data:
        if sig['name'] in waveform_summary:
            sample_waveform[sig['name']] = waveform_summary[sig['name']]
        else:
            # Create sample waveform
            waveform = []
            for t in range(0, max_time + 10, 10):
                if sig['name'] == 'clk':
                    value = '1' if (t // 10) % 2 == 0 else '0'
                elif sig['name'] == 'a':
                    value = '1' if t < 30 or (t >= 60 and t < 90) else '0'
                elif sig['name'] == 'b':
                    value = '1' if (t >= 20 and t < 50) or t >= 80 else '0'
                else:
                    value = '1' if t >= 40 and t < 70 else '0'
                waveform.append({'time': t, 'value': value})
            sample_waveform[sig['name']] = waveform
    
    waveform_json = json.dumps(sample_waveform)
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Waveform Viewer: {waveform_id}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {{
            --primary-color: #667eea;
            --secondary-color: #764ba2;
            --bg-dark: #1a1a1a;
            --bg-light: #f8f9fa;
            --text-dark: #333;
            --text-light: #666;
            --signal-high: #ff6b6b;
            --signal-low: #4ecdc4;
            --signal-unknown: #ffd166;
            --signal-highz: #06d6a0;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, var(--primary-color) 0%, var(--secondary-color) 100%);
            min-height: 100vh;
            color: var(--text-dark);
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        /* Header */
        .header {{
            background: white;
            border-radius: 12px;
            padding: 20px 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .header-info h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 5px;
            color: var(--text-dark);
        }}
        
        .header-info .subtitle {{
            font-size: 14px;
            color: var(--text-light);
        }}
        
        .header-stats {{
            display: flex;
            gap: 20px;
        }}
        
        .stat-box {{
            text-align: center;
            padding: 10px 20px;
            background: var(--bg-light);
            border-radius: 8px;
            min-width: 100px;
        }}
        
        .stat-value {{
            font-size: 24px;
            font-weight: 600;
            color: var(--primary-color);
        }}
        
        .stat-label {{
            font-size: 12px;
            color: var(--text-light);
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        
        /* Main Layout */
        .main-layout {{
            display: grid;
            grid-template-columns: 280px 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        /* Signal Panel */
        .signal-panel {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            display: flex;
            flex-direction: column;
            max-height: 700px;
        }}
        
        .signal-panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid var(--bg-light);
        }}
        
        .signal-panel-header h3 {{
            font-size: 16px;
            color: var(--text-dark);
        }}
        
        #signal-search {{
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-size: 14px;
            margin-bottom: 15px;
        }}
        
        .signal-list {{
            flex: 1;
            overflow-y: auto;
            min-height: 500px;
        }}
        
        .signal-item {{
            display: flex;
            align-items: center;
            padding: 12px 15px;
            margin-bottom: 8px;
            background: var(--bg-light);
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            border-left: 4px solid transparent;
        }}
        
        .signal-item:hover {{
            background: #e9ecef;
            transform: translateX(5px);
        }}
        
        .signal-item.selected {{
            background: #e3f2fd;
            border-left-color: var(--primary-color);
        }}
        
        .signal-color {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 12px;
            flex-shrink: 0;
        }}
        
        .signal-info {{
            flex: 1;
            min-width: 0;
        }}
        
        .signal-name {{
            font-weight: 500;
            font-size: 14px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        
        .signal-details {{
            font-size: 12px;
            color: var(--text-light);
        }}
        
        /* Waveform Panel */
        .waveform-panel {{
            background: white;
            border-radius: 12px;
            padding: 25px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            display: flex;
            flex-direction: column;
        }}
        
        .waveform-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 25px;
            padding-bottom: 15px;
            border-bottom: 2px solid var(--bg-light);
        }}
        
        .waveform-header h2 {{
            font-size: 18px;
            font-weight: 600;
            color: var(--text-dark);
        }}
        
        .controls {{
            display: flex;
            gap: 10px;
            align-items: center;
        }}
        
        .btn {{
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }}
        
        .btn-primary {{
            background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
            color: white;
        }}
        
        .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }}
        
        .btn-secondary {{
            background: var(--bg-light);
            color: var(--text-dark);
            border: 1px solid #dee2e6;
        }}
        
        .btn-secondary:hover {{
            background: #e9ecef;
        }}
        
        .btn-icon {{
            padding: 8px;
            width: 36px;
            height: 36px;
            justify-content: center;
        }}
        
        /* Waveform Display */
        .waveform-display {{
            flex: 1;
            background: var(--bg-dark);
            border-radius: 8px;
            overflow: hidden;
            position: relative;
            min-height: 500px;
        }}
        
        #waveform-container {{
            width: 100%;
            height: 100%;
            position: relative;
            overflow: auto;
        }}
        
        .time-grid {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 30px;
            background: rgba(30, 30, 30, 0.9);
            border-bottom: 1px solid #444;
            z-index: 10;
        }}
        
        .time-marker {{
            position: absolute;
            top: 0;
            width: 1px;
            height: 10px;
            background: #666;
        }}
        
        .time-label {{
            position: absolute;
            top: 12px;
            color: #aaa;
            font-size: 11px;
            font-family: monospace;
            transform: translateX(-50%);
            white-space: nowrap;
        }}
        
        .signal-rows {{
            position: relative;
            margin-top: 30px;
        }}
        
        .signal-row {{
            position: relative;
            height: 50px;
            border-bottom: 1px solid #333;
        }}
        
        .signal-label {{
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            color: white;
            font-family: monospace;
            font-weight: bold;
            font-size: 13px;
            background: rgba(0, 0, 0, 0.7);
            padding: 4px 8px;
            border-radius: 4px;
            z-index: 5;
            min-width: 80px;
            text-align: center;
        }}
        
        .waveform-canvas {{
            position: absolute;
            left: 100px;
            right: 0;
            top: 0;
            bottom: 0;
        }}
        
        /* Cursor */
        .cursor {{
            position: absolute;
            top: 30px;
            bottom: 0;
            width: 1px;
            background: #00ff00;
            z-index: 100;
            pointer-events: none;
            display: none;
        }}
        
        .cursor-time {{
            position: absolute;
            top: 5px;
            background: #00ff00;
            color: black;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: bold;
            font-family: monospace;
            transform: translateX(-50%);
            pointer-events: none;
            white-space: nowrap;
        }}
        
        /* Info Panel */
        .info-panel {{
            background: white;
            border-radius: 12px;
            padding: 25px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            margin-top: 20px;
        }}
        
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }}
        
        .info-item {{
            background: var(--bg-light);
            padding: 20px;
            border-radius: 8px;
        }}
        
        .info-label {{
            font-size: 14px;
            color: var(--text-light);
            margin-bottom: 8px;
            font-weight: 500;
        }}
        
        .info-value {{
            font-size: 18px;
            font-weight: 600;
            color: var(--text-dark);
        }}
        
        .status-badge {{
            display: inline-block;
            padding: 6px 12px;
            background: #10b981;
            color: white;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
        }}
        
        .action-buttons {{
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
        }}
        
        .btn-download {{
            background: linear-gradient(135deg, #10b981, #0da271);
            color: white;
        }}
        
        .btn-download:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(16, 185, 129, 0.4);
        }}
        
        /* Legend */
        .legend {{
            display: flex;
            gap: 20px;
            margin-top: 25px;
            padding-top: 20px;
            border-top: 1px solid #e9ecef;
            flex-wrap: wrap;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .legend-color {{
            width: 16px;
            height: 16px;
            border-radius: 4px;
        }}
        
        .legend-text {{
            font-size: 14px;
            color: var(--text-light);
        }}
        
        /* Footer */
        .footer {{
            text-align: center;
            padding: 20px;
            color: white;
            font-size: 14px;
            margin-top: 20px;
        }}
        
        .footer a {{
            color: white;
            text-decoration: none;
            font-weight: 600;
        }}
        
        .footer a:hover {{
            text-decoration: underline;
        }}
        
        /* Scrollbar */
        ::-webkit-scrollbar {{
            width: 8px;
            height: 8px;
        }}
        
        ::-webkit-scrollbar-track {{
            background: #f1f1f1;
            border-radius: 4px;
        }}
        
        ::-webkit-scrollbar-thumb {{
            background: #c1c1c1;
            border-radius: 4px;
        }}
        
        ::-webkit-scrollbar-thumb:hover {{
            background: #a8a8a8;
        }}
        
        /* Responsive */
        @media (max-width: 1200px) {{
            .main-layout {{
                grid-template-columns: 1fr;
            }}
            
            .signal-panel {{
                max-height: 300px;
            }}
        }}
        
        @media (max-width: 768px) {{
            .header {{
                flex-direction: column;
                gap: 15px;
            }}
            
            .header-stats {{
                width: 100%;
                justify-content: space-between;
            }}
            
            .stat-box {{
                min-width: 80px;
                padding: 8px 12px;
            }}
            
            .controls {{
                flex-wrap: wrap;
            }}
            
            .info-grid {{
                grid-template-columns: 1fr;
            }}
            
            .action-buttons {{
                flex-direction: column;
            }}
            
            .btn {{
                width: 100%;
                justify-content: center;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-info">
                <h1><i class="fas fa-wave-square"></i> Digital Waveform Viewer</h1>
                <div class="subtitle">ID: {waveform_id} • Timescale: {timescale}</div>
            </div>
            <div class="header-stats">
                <div class="stat-box">
                    <div class="stat-value" id="signal-count">{len(signals_data)}</div>
                    <div class="stat-label">Signals</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="max-time">{max_time}</div>
                    <div class="stat-label">{timescale}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="zoom-level">100%</div>
                    <div class="stat-label">Zoom</div>
                </div>
            </div>
        </div>
        
        <!-- Main Layout -->
        <div class="main-layout">
            <!-- Signal Panel -->
            <div class="signal-panel">
                <div class="signal-panel-header">
                    <h3><i class="fas fa-list"></i> Signals</h3>
                    <button class="btn btn-icon btn-secondary" onclick="selectAllSignals()" title="Select All">
                        <i class="fas fa-check-double"></i>
                    </button>
                </div>
                <input type="text" id="signal-search" placeholder="Search signals..." onkeyup="filterSignals()">
                <div class="signal-list" id="signal-list">
                    <!-- Signals populated by JavaScript -->
                </div>
            </div>
            
            <!-- Waveform Panel -->
            <div class="waveform-panel">
                <div class="waveform-header">
                    <h2><i class="fas fa-chart-line"></i> Waveform Display</h2>
                    <div class="controls">
                        <button class="btn btn-secondary" onclick="zoomOut()" title="Zoom Out">
                            <i class="fas fa-search-minus"></i>
                        </button>
                        <button class="btn btn-secondary" onclick="resetZoom()" title="Reset Zoom">
                            <i class="fas fa-search"></i> 100%
                        </button>
                        <button class="btn btn-secondary" onclick="zoomIn()" title="Zoom In">
                            <i class="fas fa-search-plus"></i>
                        </button>
                        <button class="btn btn-primary" onclick="refreshViewer()" title="Refresh">
                            <i class="fas fa-sync-alt"></i> Refresh
                        </button>
                    </div>
                </div>
                
                <div class="waveform-display">
                    <div id="waveform-container">
                        <div class="cursor" id="cursor">
                            <div class="cursor-time" id="cursor-time">0 ns</div>
                        </div>
                        <div class="time-grid" id="time-grid"></div>
                        <div class="signal-rows" id="signal-rows"></div>
                    </div>
                </div>
                
                <div class="controls" style="margin-top: 15px; justify-content: center;">
                    <div style="color: #666; font-size: 13px;">
                        <i class="fas fa-mouse-pointer"></i> Click to select signals • 
                        <i class="fas fa-arrows-alt-h"></i> Drag to pan • 
                        <i class="fas fa-search"></i> Scroll to zoom
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Information Panel -->
        <div class="info-panel">
            <div class="info-grid">
                <div class="info-item">
                    <div class="info-label">Waveform ID</div>
                    <div class="info-value"><code>{waveform_id}</code></div>
                </div>
                <div class="info-item">
                    <div class="info-label">Format</div>
                    <div class="info-value">VCD (Value Change Dump)</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Status</div>
                    <div class="info-value">
                        <span class="status-badge">
                            <i class="fas fa-check-circle"></i> Ready to View
                        </span>
                    </div>
                </div>
                <div class="info-item">
                    <div class="info-label">Timescale</div>
                    <div class="info-value">{timescale}</div>
                </div>
            </div>
            
            <div class="action-buttons">
                <a href="/api/waveform/{waveform_id}?download=true" class="btn btn-download">
                    <i class="fas fa-download"></i> Download VCD File
                </a>
                <button class="btn btn-secondary" onclick="copyWaveformId()">
                    <i class="fas fa-copy"></i> Copy Waveform ID
                </button>
                <button class="btn btn-secondary" onclick="exportPNG()">
                    <i class="fas fa-camera"></i> Export as PNG
                </button>
                <button class="btn btn-secondary" onclick="showHelp()">
                    <i class="fas fa-question-circle"></i> Help
                </button>
            </div>
            
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-color" style="background: var(--signal-high);"></div>
                    <div class="legend-text">Logic High (1)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: var(--signal-low);"></div>
                    <div class="legend-text">Logic Low (0)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: var(--signal-unknown);"></div>
                    <div class="legend-text">Unknown (x)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: var(--signal-highz);"></div>
                    <div class="legend-text">High-Z (z)</div>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #00ff00;"></div>
                    <div class="legend-text">Cursor</div>
                </div>
            </div>
        </div>
        
        <!-- Footer -->
        <div class="footer">
            <p>© 2024 VLSI Practice Platform • <a href="/">Back to Editor</a> • Professional Waveform Viewer</p>
        </div>
    </div>
    
    <script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
    <script>
        // Global variables
        const signalsData = {signals_json};
        const waveformData = {waveform_json};
        const timescale = {timescale_json};
        const maxTime = {max_time_json};
        
        let zoomLevel = 1.0;
        let offsetX = 0;
        let selectedSignals = [];
        let pixelsPerTime = 5; // Base scaling
        let isDragging = false;
        let dragStartX = 0;
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', function() {{
            renderSignalList();
            renderWaveform();
            setupEventListeners();
            // Auto-select first 4 signals
            setTimeout(() => {{
                const firstSignals = signalsData.slice(0, 4).map(s => s.name);
                firstSignals.forEach(signalName => {{
                    if (!selectedSignals.includes(signalName)) {{
                        toggleSignal(signalName);
                    }}
                }});
            }}, 100);
        }});
        
        // Render signal list
        function renderSignalList() {{
            const container = document.getElementById('signal-list');
            container.innerHTML = '';
            
            signalsData.forEach(signal => {{
                const div = document.createElement('div');
                div.className = 'signal-item';
                div.innerHTML = `
                    <div class="signal-color" style="background: ${{signal.color}};"></div>
                    <div class="signal-info">
                        <div class="signal-name">${{signal.short_name || signal.name}}</div>
                        <div class="signal-details">Width: ${{signal.width}} • ${{signal.type || 'wire'}}</div>
                    </div>
                `;
                
                div.dataset.signalName = signal.name;
                div.addEventListener('click', () => toggleSignal(signal.name));
                container.appendChild(div);
            }});
        }}
        
        // Filter signals based on search
        function filterSignals() {{
            const searchTerm = document.getElementById('signal-search').value.toLowerCase();
            const items = document.querySelectorAll('.signal-item');
            
            items.forEach(item => {{
                const signalName = item.dataset.signalName.toLowerCase();
                const display = signalName.includes(searchTerm) ? 'flex' : 'none';
                item.style.display = display;
            }});
        }}
        
        // Select all signals
        function selectAllSignals() {{
            const allSignals = signalsData.map(s => s.name);
            if (selectedSignals.length === allSignals.length) {{
                // Deselect all
                selectedSignals = [];
                document.querySelectorAll('.signal-item').forEach(item => {{
                    item.classList.remove('selected');
                }});
            }} else {{
                // Select all
                selectedSignals = [...allSignals];
                document.querySelectorAll('.signal-item').forEach(item => {{
                    item.classList.add('selected');
                }});
            }}
            renderWaveform();
        }}
        
        // Toggle signal selection
        function toggleSignal(signalName) {{
            const index = selectedSignals.indexOf(signalName);
            const item = document.querySelector(`.signal-item[data-signal-name="${{signalName}}"]`);
            
            if (index === -1) {{
                selectedSignals.push(signalName);
                if (item) item.classList.add('selected');
            }} else {{
                selectedSignals.splice(index, 1);
                if (item) item.classList.remove('selected');
            }}
            
            // Update signal count
            document.getElementById('signal-count').textContent = selectedSignals.length;
            renderWaveform();
        }}
        
        // Render waveform
        function renderWaveform() {{
            const container = document.getElementById('waveform-container');
            const timeGrid = document.getElementById('time-grid');
            const signalRows = document.getElementById('signal-rows');
            
            // Clear previous content
            timeGrid.innerHTML = '';
            signalRows.innerHTML = '';
            
            // Calculate dimensions
            const containerWidth = container.clientWidth;
            const totalWidth = (maxTime * pixelsPerTime * zoomLevel) + 200;
            container.style.width = Math.max(containerWidth, totalWidth) + 'px';
            
            // Render time grid
            const timeStep = calculateTimeStep();
            for (let time = 0; time <= maxTime; time += timeStep) {{
                const x = offsetX + (time * pixelsPerTime * zoomLevel);
                if (x >= -100 && x <= containerWidth + 100) {{
                    const marker = document.createElement('div');
                    marker.className = 'time-marker';
                    marker.style.left = x + 'px';
                    
                    const label = document.createElement('div');
                    label.className = 'time-label';
                    label.textContent = time + ' ' + timescale;
                    label.style.left = x + 'px';
                    
                    timeGrid.appendChild(marker);
                    timeGrid.appendChild(label);
                }}
            }}
            
            // Render selected signals
            if (selectedSignals.length === 0) {{
                const emptyMsg = document.createElement('div');
                emptyMsg.style.position = 'absolute';
                emptyMsg.style.top = '50%';
                emptyMsg.style.left = '50%';
                emptyMsg.style.transform = 'translate(-50%, -50%)';
                emptyMsg.style.color = '#666';
                emptyMsg.style.fontSize = '16px';
                emptyMsg.style.textAlign = 'center';
                emptyMsg.innerHTML = `
                    <i class="fas fa-wave-square" style="font-size: 48px; margin-bottom: 10px; display: block;"></i>
                    <div>Select signals from the left panel</div>
                    <div style="font-size: 14px; margin-top: 5px;">Click on signals to display their waveforms</div>
                `;
                signalRows.appendChild(emptyMsg);
                return;
            }}
            
            // Create signal rows
            selectedSignals.forEach((signalName, index) => {{
                const signal = signalsData.find(s => s.name === signalName);
                if (!signal) return;
                
                const row = document.createElement('div');
                row.className = 'signal-row';
                row.id = `signal-row-${{signalName}}`;
                
                // Signal label
                const label = document.createElement('div');
                label.className = 'signal-label';
                label.style.background = signal.color;
                label.textContent = signal.short_name || signal.name;
                row.appendChild(label);
                
                // Waveform canvas
                const canvas = document.createElement('canvas');
                canvas.className = 'waveform-canvas';
                canvas.id = `canvas-${{signalName}}`;
                canvas.width = totalWidth;
                canvas.height = 50;
                canvas.style.left = '100px';
                canvas.style.width = (totalWidth - 100) + 'px';
                row.appendChild(canvas);
                
                signalRows.appendChild(row);
                
                // Draw waveform
                drawSignalWaveform(signalName, canvas);
            }});
            
            // Update zoom display
            document.getElementById('zoom-level').textContent = Math.round(zoomLevel * 100) + '%';
        }}
        
        // Calculate appropriate time step based on zoom
        function calculateTimeStep() {{
            if (zoomLevel < 0.3) return 50;
            if (zoomLevel < 0.7) return 20;
            if (zoomLevel < 1.5) return 10;
            if (zoomLevel < 3) return 5;
            return 2;
        }}
        
        // Draw waveform for a specific signal
        function drawSignalWaveform(signalName, canvas) {{
            const ctx = canvas.getContext('2d');
            const width = canvas.width;
            const height = canvas.height;
            
            // Clear canvas
            ctx.clearRect(0, 0, width, height);
            
            // Get waveform data
            const waveform = waveformData[signalName];
            if (!waveform || waveform.length === 0) return;
            
            // Sort by time
            waveform.sort((a, b) => a.time - b.time);
            
            // Draw waveform
            let lastX = null;
            let lastY = null;
            let lastValue = null;
            
            for (let i = 0; i < waveform.length; i++) {{
                const point = waveform[i];
                const nextPoint = waveform[i + 1];
                const x = offsetX + (point.time * pixelsPerTime * zoomLevel);
                const value = point.value;
                
                // Determine Y position based on value
                let y;
                if (value === '1' || value === '1') {{
                    y = height * 0.3; // High position
                }} else if (value === '0' || value === '0') {{
                    y = height * 0.7; // Low position
                }} else if (value === 'z' || value === 'Z') {{
                    y = height * 0.5; // Middle for high-Z
                }} else {{
                    y = height * 0.5; // Middle for unknown
                }}
                
                // Draw horizontal segment
                if (lastX !== null) {{
                    const endX = x;
                    
                    // Set line style based on value
                    if (lastValue === '1') {{
                        ctx.strokeStyle = '#ff6b6b';
                        ctx.lineWidth = 3;
                        ctx.setLineDash([]);
                    }} else if (lastValue === '0') {{
                        ctx.strokeStyle = '#4ecdc4';
                        ctx.lineWidth = 3;
                        ctx.setLineDash([]);
                    }} else if (lastValue === 'z') {{
                        ctx.strokeStyle = '#06d6a0';
                        ctx.lineWidth = 2;
                        ctx.setLineDash([5, 3]);
                    }} else {{
                        ctx.strokeStyle = '#ffd166';
                        ctx.lineWidth = 2;
                        ctx.setLineDash([3, 3]);
                    }}
                    
                    ctx.beginPath();
                    ctx.moveTo(lastX, lastY);
                    ctx.lineTo(endX, lastY);
                    ctx.stroke();
                    
                    // Reset line dash
                    ctx.setLineDash([]);
                }}
                
                // Draw vertical transition if value changes
                if (nextPoint && value !== nextPoint.value) {{
                    ctx.strokeStyle = '#888';
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(x, height * 0.2);
                    ctx.lineTo(x, height * 0.8);
                    ctx.stroke();
                }}
                
                lastX = x;
                lastY = y;
                lastValue = value;
            }}
            
            // Draw final segment
            if (lastX !== null) {{
                const endX = offsetX + (maxTime * pixelsPerTime * zoomLevel);
                if (endX > lastX) {{
                    // Use same style as last segment
                    if (lastValue === '1') {{
                        ctx.strokeStyle = '#ff6b6b';
                        ctx.lineWidth = 3;
                        ctx.setLineDash([]);
                    }} else if (lastValue === '0') {{
                        ctx.strokeStyle = '#4ecdc4';
                        ctx.lineWidth = 3;
                        ctx.setLineDash([]);
                    }} else if (lastValue === 'z') {{
                        ctx.strokeStyle = '#06d6a0';
                        ctx.lineWidth = 2;
                        ctx.setLineDash([5, 3]);
                    }} else {{
                        ctx.strokeStyle = '#ffd166';
                        ctx.lineWidth = 2;
                        ctx.setLineDash([3, 3]);
                    }}
                    
                    ctx.beginPath();
                    ctx.moveTo(lastX, lastY);
                    ctx.lineTo(endX, lastY);
                    ctx.stroke();
                }}
            }}
        }}
        
        // Setup event listeners for interaction
        function setupEventListeners() {{
            const container = document.getElementById('waveform-container');
            const cursor = document.getElementById('cursor');
            const cursorTime = document.getElementById('cursor-time');
            
            // Mouse wheel for zoom
            container.addEventListener('wheel', function(e) {{
                e.preventDefault();
                
                const rect = container.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const mouseTime = (mouseX - offsetX) / (pixelsPerTime * zoomLevel);
                
                const delta = e.deltaY > 0 ? 0.9 : 1.1;
                const newZoom = Math.max(0.1, Math.min(5, zoomLevel * delta));
                
                if (newZoom !== zoomLevel) {{
                    zoomLevel = newZoom;
                    // Keep mouse position fixed
                    offsetX = mouseX - (mouseTime * pixelsPerTime * zoomLevel);
                    renderWaveform();
                }}
            }});
            
            // Mouse drag for panning
            container.addEventListener('mousedown', function(e) {{
                isDragging = true;
                dragStartX = e.clientX - offsetX;
                container.style.cursor = 'grabbing';
            }});
            
            document.addEventListener('mousemove', function(e) {{
                if (isDragging) {{
                    offsetX = e.clientX - dragStartX;
                    renderWaveform();
                }}
                
                // Show cursor with time
                const rect = container.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const time = Math.round((mouseX - offsetX) / (pixelsPerTime * zoomLevel));
                
                if (time >= 0 && time <= maxTime) {{
                    cursor.style.left = mouseX + 'px';
                    cursor.style.display = 'block';
                    cursorTime.textContent = time + ' ' + timescale;
                    cursorTime.style.left = mouseX + 'px';
                }} else {{
                    cursor.style.display = 'none';
                }}
            }});
            
            document.addEventListener('mouseup', function() {{
                isDragging = false;
                container.style.cursor = 'default';
            }});
            
            // Touch events for mobile
            let touchStartX = 0;
            let touchStartOffset = 0;
            
            container.addEventListener('touchstart', function(e) {{
                if (e.touches.length === 1) {{
                    touchStartX = e.touches[0].clientX;
                    touchStartOffset = offsetX;
                    e.preventDefault();
                }}
            }});
            
            container.addEventListener('touchmove', function(e) {{
                if (e.touches.length === 1) {{
                    const touchX = e.touches[0].clientX;
                    offsetX = touchStartOffset + (touchX - touchStartX);
                    renderWaveform();
                    e.preventDefault();
                }}
            }});
        }}
        
        // Zoom functions
        function zoomIn() {{
            zoomLevel = Math.min(5, zoomLevel * 1.2);
            renderWaveform();
        }}
        
        function zoomOut() {{
            zoomLevel = Math.max(0.1, zoomLevel / 1.2);
            renderWaveform();
        }}
        
        function resetZoom() {{
            zoomLevel = 1.0;
            offsetX = 0;
            renderWaveform();
        }}
        
        // Utility functions
        function refreshViewer() {{
            window.location.reload();
        }}
        
        function copyWaveformId() {{
            navigator.clipboard.writeText('{waveform_id}')
                .then(() => alert('Waveform ID copied to clipboard!'))
                .catch(() => alert('Failed to copy'));
        }}
        
        function exportPNG() {{
            const container = document.getElementById('waveform-container');
            html2canvas(container, {{
                backgroundColor: '#1a1a1a',
                scale: 2,
                logging: false
            }}).then(canvas => {{
                const link = document.createElement('a');
                link.download = 'waveform-{waveform_id}.png';
                link.href = canvas.toDataURL('image/png');
                link.click();
            }});
        }}
        
        function showHelp() {{
            alert(`Waveform Viewer Help:
            
1. Signal Selection:
   - Click signals in the left panel to show/hide waveforms
   - Use search box to filter signals
   
2. Navigation:
   - Scroll to zoom in/out
   - Click and drag to pan horizontally
   - Use zoom buttons for precise control
   
3. Features:
   - Green cursor shows time position
   - Different colors for signal states:
     • Red: Logic High (1)
     • Blue: Logic Low (0)
     • Yellow: Unknown (x)
     • Green: High-Z (z)
   
4. Export:
   - Download original VCD file
   - Export waveform as PNG image
   - Copy waveform ID for sharing`);
        }}
    </script>
</body>
</html>'''

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "waveforms": len(list(WAVEFORM_DIR.glob("*.vcd"))),
        "problems": len(PROBLEMS)
    }


class CustomRunRequest(BaseModel):
    user_code: str
    testbench: str
    generate_waveform: bool = False


@app.post("/api/dev/run-custom")
@limiter.limit("20/minute")
async def run_custom(request: Request, body: CustomRunRequest):
    """Run arbitrary Verilog code + testbench — no problem_id needed (Sandbox/Builder)"""
    try:
        try:
            await asyncio.wait_for(sim_semaphore.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Server busy. Try again in a moment.")

        try:
            result = run_simulation(
                body.user_code,
                body.testbench,
                body.generate_waveform,
                problem_title="custom"
            )
        finally:
            sim_semaphore.release()

        response = {
            "success": result["success"],
            "passed": result.get("passed", False),
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "details": result.get("details", ""),
            "message": result.get("message", "")
        }
        if "waveform_id" in result:
            response["waveform_id"] = result["waveform_id"]
            response["waveform_url"] = f"/api/waveform/{result['waveform_id']}"

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in run_custom: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dev/audit")
async def audit_problems():
    """Batch-test all problems — compiles + runs each testbench, returns pass/fail table"""
    if not PROBLEMS:
        return {"results": [], "summary": {"total": 0, "pass": 0, "fail": 0}}

    results = []
    passed_count = 0

    for problem in PROBLEMS:
        pid = problem.get("id", "unknown")
        title = problem.get("title", pid)
        difficulty = problem.get("difficulty", "unknown")
        testbench = problem.get("testbench", "")
        template = problem.get("template", "")

        if not testbench:
            results.append({
                "id": pid, "title": title, "difficulty": difficulty,
                "status": "fail", "time": 0, "notes": "No testbench defined"
            })
            continue

        start = time.time()
        try:
            # Audit mode: run template code (empty shell) against testbench
            # We're checking the testbench itself compiles and runs cleanly
            result = run_simulation(
                user_code=template,
                testbench=testbench,
                generate_waveform=False,
                problem_title=title
            )
            elapsed = round(time.time() - start, 2)

            if not result["success"]:
                status = "fail"
                notes = result.get("details", result.get("error", ""))[:120]
            else:
                status = "pass"
                notes = ""
            
            if status == "pass":
                passed_count += 1

            results.append({
                "id": pid, "title": title, "difficulty": difficulty,
                "status": status, "time": elapsed, "notes": notes
            })

        except Exception as e:
            elapsed = round(time.time() - start, 2)
            results.append({
                "id": pid, "title": title, "difficulty": difficulty,
                "status": "fail", "time": elapsed, "notes": str(e)[:120]
            })

    return {
        "results": results,
        "summary": {
            "total": len(PROBLEMS),
            "pass": passed_count,
            "fail": len(PROBLEMS) - passed_count
        }
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
    
