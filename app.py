#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Fixed with Test Pass Detection
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field
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

# Create waveform directory (falls back to the OS temp dir on Windows/dev)
WAVEFORM_DIR = Path(os.environ.get("WAVEFORM_DIR", "/tmp/waveforms"))
try:
    WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)
except Exception:
    import tempfile as _tempfile
    WAVEFORM_DIR = Path(_tempfile.gettempdir()) / "chipversity_waveforms"
    WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"Waveform directory: {WAVEFORM_DIR}")

# Shareable-project store (falls back to OS temp dir on Windows/dev)
SHARE_DIR = WAVEFORM_DIR.parent / "chipversity_shares"
try:
    SHARE_DIR.mkdir(exist_ok=True, parents=True)
except Exception:
    SHARE_DIR = WAVEFORM_DIR / "shares"
    SHARE_DIR.mkdir(exist_ok=True, parents=True)

logger.info(f"Share directory: {SHARE_DIR}")

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


# Canonical category metadata — display title, sort order, icon, colour, blurb.
# Keeps the /api/categories response stable and consistently ordered.
CATEGORY_META = {
    "basic_gates": (
        "Basic Logic Gates", 1, "fas fa-gamepad", "#4f46e5",
        "Design the fundamental gates — AND, OR, NOT, NAND, NOR, XOR, XNOR. "
        "The building blocks every other circuit is made from."),
    "arithmetic": (
        "Arithmetic Circuits", 2, "fas fa-calculator", "#ef4444",
        "Build adders, subtractors and multipliers, from half/full adders through "
        "ripple-carry and carry look-ahead to array multipliers."),
    "combinational": (
        "Combinational Logic", 3, "fas fa-project-diagram", "#10b981",
        "Multiplexers, demultiplexers, encoders, decoders, comparators and shifters — "
        "data selection, routing and manipulation with no clock."),
    "parity": (
        "Parity & Error Detection", 4, "fas fa-shield-alt", "#8b5cf6",
        "Parity generators and checkers for single-bit error detection, plus the "
        "XOR reduction techniques behind them."),
    "converters": (
        "Code Converters", 5, "fas fa-exchange-alt", "#06b6d4",
        "Binary to Gray and back, BCD, excess-3, seven-segment and one-hot encodings — "
        "translation between numeric representations."),
    "sequential": (
        "Sequential Logic", 6, "fas fa-history", "#f59e0b",
        "Latches and flip-flops (D, JK, T, SR) with synchronous and asynchronous resets, "
        "registers and edge detectors."),
    "shift_registers": (
        "Shift Registers", 7, "fas fa-arrow-right", "#3b82f6",
        "SISO, SIPO, PISO and PIPO registers, barrel shifters and shift-register based "
        "datapaths."),
    "counters": (
        "Counters", 8, "fas fa-sort-numeric-up", "#ec4899",
        "Ripple, synchronous, up/down, ring, Johnson and modulo-N counters, plus "
        "arbitrary sequence generators."),
    "pattern_generation": (
        "Pattern & Sequence Generation", 9, "fas fa-wave-square", "#0ea5e9",
        "Walking-one, ring, triangle and other deterministic bit patterns — "
        "stimulus and carrier generators built from a state register."),
    "timing": (
        "Clocking & Timing", 10, "fas fa-clock", "#14b8a6",
        "Clock dividers, frequency dividers, CDC synchronisers and timing-related "
        "building blocks."),
    "memory": (
        "Memory Elements", 11, "fas fa-memory", "#a855f7",
        "RAM, ROM, register files, stacks and FIFOs — storage elements and the "
        "handshaking around them."),
    "fsm": (
        "Finite State Machines", 12, "fas fa-sitemap", "#22c55e",
        "Mealy and Moore machines for sequence detection, traffic lights, arbiters and "
        "other state-based controllers."),
    "testbench": (
        "Testbench & Simulation", 13, "fas fa-vial", "#f97316",
        "Writing stimulus, clock generation, checkers and self-checking testbenches — "
        "the verification side of RTL design."),
    "advanced": (
        "Advanced Circuits", 14, "fas fa-rocket", "#6366f1",
        "ALUs, arbiters, pipelines and other larger designs that combine the earlier "
        "building blocks."),
    "interview_puzzle": (
        "Interview Puzzles", 15, "fas fa-brain", "#d946ef",
        "The questions that actually get asked in hardware interviews — CDC "
        "synchronisers, arbiters, divide-by-N with 50% duty, Hamming codes and the "
        "classic \"how would you do this in RTL?\" brain-teasers."),
    "real_world": (
        "Real-World Interfaces", 16, "fas fa-industry", "#0d9488",
        "Protocols and interfaces as they appear in production silicon — UART, SPI, "
        "I2C, handshakes and bus behaviour."),
    "story": (
        "Applied Scenarios", 17, "fas fa-book-open", "#f43f5e",
        "Everyday systems described as a story — vending machines, traffic lights, "
        "washing machines, ATMs — then designed as a real RTL block."),
}
_DEFAULT_CAT_META = ("Other", 99, "fas fa-microchip", "#64748b", "")


def _category_list():
    """Build the canonical category list from the loaded problems."""
    seen = {}
    for p in PROBLEMS:
        cid = (p.get("category") or "").strip()
        if not cid:
            continue
        seen[cid] = seen.get(cid, 0) + 1
    cats = []
    for cid, count in seen.items():
        meta = CATEGORY_META.get(cid)
        if meta:
            title, order, icon, color, blurb = meta
        else:
            title = cid.replace("_", " ").title()
            order, icon, color, blurb = _DEFAULT_CAT_META[1:]
        if not blurb:
            blurb = f"{count} problem{'s' if count != 1 else ''} in this topic."
        cats.append({
            "id": cid,
            "title": title,
            "description": blurb,
            "icon": icon,
            "color": color,
            "order": order,
            "problem_count": count,
        })
    cats.sort(key=lambda c: (c["order"], c["title"]))
    return cats


@app.get("/api/categories")
async def get_categories():
    """Return the canonical problem categories (id, title, order, count)."""
    cats = _category_list()
    return {"categories": cats, "total": len(cats)}


@app.get("/api/svproblems")
async def get_sv_problems():
    """SystemVerilog problem set.

    This deployment ships Verilog problems only; SystemVerilog problems live in
    the separate SV backend. Return an empty, well-formed list so the SV pages
    fall back gracefully instead of receiving a 404.
    """
    sv = [p for p in PROBLEMS if str(p.get("language", "")).lower() == "systemverilog"]
    simplified = [{
        "id": p["id"],
        "title": p["title"],
        "description": p.get("description", ""),
        "difficulty": p.get("difficulty", "medium"),
        "category": p.get("category", ""),
        "language": "systemverilog",
        "template": p.get("template", ""),
        "hint": p.get("hint", ""),
        "examples": p.get("examples", []),
        "constraints": p.get("constraints", []),
        "test_cases": p.get("test_cases", []),
    } for p in sv]
    return {"problems": simplified, "total": len(simplified)}

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

        # Strip ALL `timescale directives from user_code and testbench FIRST.
        # Handles: `timescale 1ns/1ps  OR  `timescale 1ns / 1ps  OR  with trailing comment
        # We inject one canonical `timescale 1ns/1ps at the very top.
        # NOTE: Stripping must happen BEFORE waveform injection so the injected
        # dump block (which has no timescale) doesn't accidentally get stripped.
        timescale_re = re.compile(r'`timescale\s+\w+\s*/\s*\w+[^\n]*\n?')
        user_code_clean = timescale_re.sub('', user_code)
        testbench_clean = timescale_re.sub('', testbench)

        # Inject VCD dump so waveform viewer works (operates on the already-cleaned testbench)
        if generate_waveform:
            waveform_id = str(uuid.uuid4())
            vcd_path = str(tmp_path / "waveform.vcd").replace("\\", "/")
            if "$dumpfile" in testbench_clean:
                # Testbench already calls $dumpfile — just rewrite the path to our temp dir
                testbench_clean = re.sub(
                    r'\$dumpfile\s*\(\s*"[^"]*"\s*\)',
                    f'$dumpfile("{vcd_path}")',
                    testbench_clean
                )
            else:
                # Inject dump block INSIDE the last module, just before its endmodule
                tb_mod_match = re.findall(r'^\s*module\s+(\w+)', testbench_clean, re.MULTILINE)
                tb_mod = tb_mod_match[-1] if tb_mod_match else None
                dumpvars_line = f'    $dumpvars(0, {tb_mod});\n' if tb_mod else '    $dumpvars(0);\n'
                dump_block = (
                    f'\ninitial begin\n'
                    f'    $dumpfile("{vcd_path}");\n'
                    f'{dumpvars_line}'
                    f'end\n'
                )
                # Insert before the last endmodule in testbench
                last_end = testbench_clean.rfind('endmodule')
                if last_end != -1:
                    testbench_clean = testbench_clean[:last_end] + dump_block + testbench_clean[last_end:]
                else:
                    testbench_clean = testbench_clean + dump_block

        # Combine source
        source = f"`timescale 1ns/1ps\n{user_code_clean}\n{testbench_clean}"
        source_file = tmp_path / "design.v"
        source_file.write_text(source)

        # Compile
        # NOTE: a missing toolchain must degrade to a clean JSON error, not an
        # unhandled FileNotFoundError bubbling up as HTTP 500.
        output_exec = tmp_path / "sim"
        try:
            compile_result = subprocess.run(
                ["iverilog", "-g2012", "-o", str(output_exec), str(source_file)],
                capture_output=True,
                text=True,
                timeout=30
            )
        except FileNotFoundError:
            return {
                "success": False,
                "passed": False,
                "error": "Icarus Verilog not installed",
                "details": "iverilog/vvp are unavailable on this server."
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "passed": False,
                "error": "Compile Timeout",
                "details": "Compilation exceeded 30 seconds."
            }

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
        except FileNotFoundError:
            return {
                "success": False,
                "passed": False,
                "error": "Icarus Verilog not installed",
                "details": "The vvp runtime is unavailable on this server."
            }
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

        # Pass/fail detection.
        # FAIL is evaluated before PASS: a testbench often prints "PASS: test 1"
        # and "FAIL: test 2", and any FAIL must mean the submission failed.
        # (The previous `"PASS" in output.upper()` check passed such cases.)
        passed, message = _detect_pass(output, sim_result.returncode)
        if not passed:
            err_lines = [ln for ln in output.splitlines()
                         if "error" in ln.lower() or "fail" in ln.lower()]
            if err_lines:
                message = err_lines[0].strip()[:160] or message

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
        self.id_to_name = {}   # var_id -> full hierarchical name (built during parse)
        
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
                    # VCD timescale may be multi-token: "$timescale 1 ns $end" or "$timescale 1ns/1ps $end"
                    # Strip the $timescale keyword and the trailing $end, join remaining tokens
                    ts_line = line.replace('$timescale', '').replace('$end', '').strip()
                    if ts_line:
                        self.timescale = ts_line
                    elif line == '$timescale':
                        # timescale value is on the next lines — store marker and handle below
                        self.timescale = '1ns'  # safe default; multi-line VCDs are rare in iverilog
                
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
            self.id_to_name = signal_map
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
        """Record signal states at a specific time.

        Uses the prebuilt id -> name map (self.id_to_name) instead of scanning
        the whole signal list per signal, which made this O(signals^2) per step.
        """
        for sig_id, value in signal_values.items():
            signal_name = self.id_to_name.get(sig_id)
            if signal_name is None:
                continue
            waveform = self.waveform_data.get(signal_name)
            if waveform is None:
                continue
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
    
    # No synthetic/demo data: if the VCD is missing or empty we render an
    # explicit "no waveform data" state rather than inventing fake signals.
    has_data = bool(signals_data)

    # Prepare data for JavaScript
    signals_json = json.dumps(signals_data)
    timescale_json = json.dumps(timescale)
    max_time_json = json.dumps(max_time)

    # Only real recorded waveform data is sent to the viewer.
    waveform_json = json.dumps(waveform_summary)
    has_data_json = json.dumps(has_data)
    
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
        
        /* No-data notice */
        .no-data-banner {{
            display: none;
            background: #fff7ed;
            border: 1px solid #fdba74;
            border-left: 6px solid #f97316;
            border-radius: 12px;
            padding: 22px 26px;
            margin-bottom: 20px;
            color: #7c2d12;
            box-shadow: 0 10px 25px rgba(0,0,0,0.08);
        }}
        
        .no-data-banner h2 {{
            font-size: 18px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .no-data-banner p {{
            font-size: 14px;
            line-height: 1.6;
            color: #9a3412;
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
        <!-- No-data notice (shown only when the VCD is missing or empty) -->
        <div class="no-data-banner" id="no-data-banner">
            <h2><i class="fas fa-exclamation-triangle"></i> No waveform data</h2>
            <p>
                No VCD was recorded for this run. Enable the <strong>Waveform</strong>
                option before running the simulation, and make sure the testbench
                reaches a <code>$finish</code> so the dump is written.
            </p>
        </div>
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
        const hasData = {has_data_json};
        
        let zoomLevel = 1.0;
        let offsetX = 0;
        let selectedSignals = [];
        let pixelsPerTime = 5; // Base scaling
        let isDragging = false;
        let dragStartX = 0;
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', function() {{
            if (!hasData || signalsData.length === 0) {{
                const banner = document.getElementById('no-data-banner');
                if (banner) banner.style.display = 'block';
                const grid = document.querySelector('.main-layout');
                if (grid) grid.style.opacity = '0.35';
                return;
            }}
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
                if (value === '1') {{
                    y = height * 0.3; // High position
                }} else if (value === '0') {{
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
        "problems": len(PROBLEMS),
        "engines": {
            "iverilog": bool(shutil.which("iverilog")) and bool(shutil.which("vvp")),
            "verilator": bool(shutil.which("verilator")),
            "verilator_coverage": bool(shutil.which("verilator_coverage")),
            "yosys": bool(shutil.which("yosys"))
        }
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

    # Without a toolchain every problem would be reported as a failure, which is
    # misleading. Short-circuit with an explicit explanation instead.
    if not (shutil.which("iverilog") and shutil.which("vvp")):
        return {
            "results": [],
            "summary": {"total": len(PROBLEMS), "pass": 0, "fail": 0,
                        "skipped": len(PROBLEMS)},
            "error": "Icarus Verilog is not installed on this server — the audit cannot run.",
        }

    results = []
    passed_count = 0

    for problem in PROBLEMS:
        pid = problem.get("id", "unknown")
        title = problem.get("title", pid)
        difficulty = problem.get("difficulty", "unknown")
        testbench = problem.get("testbench", "")
        template = problem.get("template", "")
        solution = (problem.get("solution") or "").strip()
        # Verify against the reference solution when present, otherwise the
        # starter template (legacy problems). A correct problem passes here.
        user_code = solution if solution else template

        if not testbench:
            results.append({
                "id": pid, "title": title, "difficulty": difficulty,
                "status": "fail", "time": 0, "notes": "No testbench defined"
            })
            continue

        start = time.time()
        try:
            result = run_simulation(
                user_code=user_code,
                testbench=testbench,
                generate_waveform=False,
                problem_title=title
            )
            elapsed = round(time.time() - start, 2)

            if not result["success"]:
                status = "fail"
                notes = result.get("details", result.get("error", ""))[:120]
            elif not result.get("passed", False):
                status = "fail"
                notes = (result.get("message") or "reference solution did not pass")[:120]
            else:
                status = "pass"
                notes = "solution" if solution else "template"

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


# ============================================================
# Advanced multi-file simulator — Icarus Verilog + Verilator
# Enables the full toolchain: multi-file designs, all Icarus
# language generations, defines/parameters/flags, SystemVerilog,
# VCD waveforms, and Verilator assertions + coverage.
# ============================================================

ALLOWED_GENERATIONS = {
    "1995": "-g1995",
    "2001": "-g2001",
    "2005": "-g2005",
    "2009": "-g2009",
    "2012": "-g2012",
    "systemverilog": "-g2012",
    "sv": "-g2012",
}

ALLOWED_EXT = {".v", ".sv", ".vh", ".svh", ".vhdl"}

# Only these flag families are forwarded to iverilog (blocks -o/-I/-y/-f/... abuse)
_ALLOWED_FLAG_RE = re.compile(r'^(-g|-D|-W|-p|-P)[A-Za-z0-9_=.:,+\-]*$')
_DANGEROUS_PREFIXES = ("-o", "-I", "-y", "-Y", "-f", "-c", "-M", "-m", "-T", "-S", "-E")

_NAME_RE = re.compile(r'^[A-Za-z_]\w*$')
_DEFINE_RE = re.compile(r'^[A-Za-z_]\w*(=[A-Za-z0-9_ .:+\-/]*)?$')
# -P elaboration parameter overrides: path.to.param=value
_PARAM_RE = re.compile(r'^[A-Za-z_][\w.\[\]]*=[A-Za-z0-9_ .:+\-\'/]*$')
# runtime plusargs (without the leading '+')
_PLUSARG_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_=./:+\-]*$')
# safe vvp runtime flags (no arguments — avoids arbitrary file writes)
_VVP_FLAGS_ALLOWED = {"-n", "-v", "-N", "-s", "-d"}
_MODULE_DEF_RE = re.compile(r'\bmodule\s+(\w+)')
_MODULE_INST_RE = re.compile(
    r'^\s*([A-Za-z_]\w*)\s*(?:#\s*\([^;]*?\)\s*)?([A-Za-z_]\w*)\s*\(', re.MULTILINE)


def _sanitize_flags(raw: str) -> list:
    """Return only safe iverilog flag tokens from a raw string."""
    if not raw:
        return []
    flags = []
    for tok in raw.split():
        if tok.startswith(_DANGEROUS_PREFIXES):
            continue
        if _ALLOWED_FLAG_RE.match(tok):
            flags.append(tok)
    return flags


def _safe_filename(name: str, index: int) -> str:
    """Return a safe basename ending in an allowed HDL extension."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    if not base:
        base = f"file{index}.v"
    root, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        ext = ".v"
    root = re.sub(r'[^A-Za-z0-9_\-]', '_', root)[:64] or f"file{index}"
    return root + ext


def _strip_comments(code: str) -> str:
    code = re.sub(r'/\*.*?\*/', ' ', code, flags=re.DOTALL)
    code = re.sub(r'//[^\n]*', ' ', code)
    return code


def detect_top_module(sources: dict) -> str:
    """Pick the most likely top module: a module that is never instantiated
    (preferring testbench-like names). Falls back to the last module defined."""
    defined = []
    instantiated = set()
    for name, content in sources.items():
        clean = _strip_comments(content or "")
        for m in _MODULE_DEF_RE.finditer(clean):
            defined.append(m.group(1))
        for m in _MODULE_INST_RE.finditer(clean):
            instantiated.add(m.group(1))

    roots = [d for d in defined if d not in instantiated]
    for d in roots:
        low = d.lower()
        if low.startswith("tb") or "testbench" in low or low.startswith("top"):
            return d
    if roots:
        return roots[-1]
    return defined[-1] if defined else ""


def _inject_vcd(code: str, vcd_path: str) -> str:
    """Force VCD dumping inside the given testbench/module source."""
    if "$dumpfile" in code:
        return re.sub(
            r'\$dumpfile\s*\(\s*"[^"]*"\s*\)',
            f'$dumpfile("{vcd_path}")',
            code
        )
    mods = re.findall(r'^\s*module\s+(\w+)', code, re.MULTILINE)
    tb_mod = mods[-1] if mods else None
    dumpvars = f'    $dumpvars(0, {tb_mod});\n' if tb_mod else '    $dumpvars(0);\n'
    block = (
        f'\ninitial begin\n'
        f'    $dumpfile("{vcd_path}");\n'
        f'{dumpvars}'
        f'end\n'
    )
    last_end = code.rfind('endmodule')
    if last_end != -1:
        return code[:last_end] + block + code[last_end:]
    return code + block


def _detect_pass(output: str, returncode: int):
    """Shared PASS/FAIL detection for iverilog + verilator output."""
    up = output.upper()
    if re.search(r'\bFAIL(ED|URE)?\b', up):
        return False, "Tests failed"
    # Use case-sensitive check for Verilator's %Error prefix to avoid matching
    # %Warning-SOMEERROR or other diagnostic codes that contain "ERROR" in their name.
    if re.search(r'%Error', output) or re.search(r'\bRUNTIME ERROR\b', up):
        return False, "Runtime error"
    # Icarus / vvp runtime diagnostics, e.g. "design.v:12: ERROR: ..." or
    # "$fatal" — these must not be reported as a successful run.
    if re.search(r'(?m)^\s*(?:\S+:\d+:\s*)?ERROR\s*:', output) or "$fatal" in output.lower():
        return False, "Runtime error"
    if re.search(r'\bPASS(ED)?\b', up):
        return True, "All tests passed!"
    if "SIMULATION FINISHED" in up or "RUNTIME FINISHED" in up:
        return True, "Simulation completed successfully"
    if returncode not in (0, 1):
        return False, "Non-zero exit code"
    return True, "Code executed successfully (verify output)"


def _read_verilator_coverage(tmp: Path):
    """Parse a Verilator coverage.dat into an LCOV-style line summary."""
    dat = None
    for pattern in ("coverage.dat", "**/coverage.dat"):
        found = list(tmp.glob(pattern))
        if found:
            dat = found[0]
            break
    if not dat:
        return None
    info = tmp / "chipversity_coverage.info"
    try:
        subprocess.run(
            ["verilator_coverage", "--write-info", str(info), str(dat)],
            capture_output=True, text=True, timeout=30
        )
    except Exception:
        return {"note": "coverage.dat generated", "file": dat.name,
                "size": dat.stat().st_size}
    found = hit = 0
    if info.exists():
        for line in info.read_text(errors="ignore").splitlines():
            if line.startswith("LF:"):
                found += int(line[3:].strip() or 0)
            elif line.startswith("LH:"):
                hit += int(line[3:].strip() or 0)
    pct = round(100.0 * hit / found, 1) if found else 0.0
    return {"lines_found": found, "lines_hit": hit,
            "coverage_percent": pct, "file": dat.name}


def _collect_waveform(tmp: Path, waveform_id: str, result: dict) -> dict:
    if not waveform_id:
        return result
    vcd_file = tmp / "waveform.vcd"
    if vcd_file.exists() and vcd_file.stat().st_size > 0:
        dest = WAVEFORM_DIR / f"{waveform_id}.vcd"
        shutil.copy2(vcd_file, dest)
        result["waveform_id"] = waveform_id
        logger.info(f"Waveform saved: {waveform_id} ({vcd_file.stat().st_size} bytes)")
    else:
        logger.warning("VCD file missing or empty (advanced run)")
    return result


def _run_iverilog(tmp: Path, sources: list, top: str, generation: str,
                  defines: list, extra_flags: list, waveform_id: str,
                  params: list = None, plusargs: list = None,
                  vvp_flags: list = None, preprocess_only: bool = False,
                  coverage: bool = False) -> dict:
    params = params or []
    plusargs = plusargs or []
    vvp_flags = vvp_flags or []
    gen_flag = ALLOWED_GENERATIONS.get(str(generation).lower(), "-g2012")
    out = tmp / "sim.out"

    # Common compile inputs
    compile_opts = [gen_flag]
    if top and not preprocess_only:
        compile_opts += ["-s", top]
    for d in defines:
        if _DEFINE_RE.match(d):
            compile_opts.append("-D" + d)
    for prm in params:
        if _PARAM_RE.match(prm):
            compile_opts.append("-P" + prm)
    # Include directories: the temp workspace (so `include "defs.vh" resolves)
    compile_opts += ["-I", str(tmp), "-I", "."]
    compile_opts += extra_flags

    # ---- Preprocess only (-E) ----
    if preprocess_only:
        cmd = ["iverilog", "-E"] + compile_opts + [str(p) for p in sources]
        try:
            pp = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        except FileNotFoundError:
            return {"success": False, "passed": False,
                    "error": "Icarus Verilog not installed",
                    "details": "iverilog is unavailable on the server."}
        except subprocess.TimeoutExpired:
            return {"success": False, "passed": False, "error": "Preprocess Timeout",
                    "details": "Preprocessing exceeded 45 seconds."}
        if pp.returncode != 0:
            return {"success": False, "passed": False, "error": "Preprocess Failed",
                    "details": (pp.stderr or pp.stdout or "Unknown error")[:4000]}
        text = pp.stdout or ""
        return {
            "success": True,
            "passed": True,
            "output": text[:12000],
            "error": "",
            "details": "",
            "message": "Preprocessed source (-E)",
            "engine": "iverilog",
            "top_module": top,
            "preprocess_only": True,
        }

    # ---- Compile ----
    cmd = ["iverilog"] + compile_opts + ["-o", str(out)] + [str(p) for p in sources]
    try:
        comp = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except FileNotFoundError:
        return {"success": False, "passed": False,
                "error": "Icarus Verilog not installed",
                "details": "iverilog/vvp are unavailable on the server."}
    except subprocess.TimeoutExpired:
        return {"success": False, "passed": False, "error": "Compile Timeout",
                "details": "Compilation exceeded 45 seconds."}

    if comp.returncode != 0 or not out.exists():
        return {"success": False, "passed": False, "error": "Compilation Failed",
                "details": (comp.stderr or comp.stdout or "Unknown error")[:4000]}

    # ---- Simulate (vvp) ----
    vvp_cmd = ["vvp"] + [f for f in vvp_flags if f in _VVP_FLAGS_ALLOWED]
    vvp_cmd += [str(out)] + ["+" + p for p in plusargs]
    try:
        sim = subprocess.run(
            vvp_cmd, capture_output=True, text=True, timeout=20,
            cwd=str(tmp),
            preexec_fn=_set_resource_limits if sys.platform != "win32" else None
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "passed": False, "error": "Simulation Timeout",
                "details": "Simulation exceeded 20 seconds. Check for infinite loops."}

    compile_notes = (comp.stderr or "").strip()
    output = sim.stdout + sim.stderr
    if compile_notes:
        output = compile_notes + "\n" + output
    passed, message = _detect_pass(output, sim.returncode)
    result = {
        "success": True,
        "passed": passed,
        "output": output[:8000],
        "error": "",
        "details": "",
        "message": message,
        "engine": "iverilog",
        "top_module": top,
        "compile_warnings": compile_notes[:2000],
    }
    if coverage:
        # Use the actual VCD path (tmp / "waveform.vcd") — this file is written by vvp
        # when either generate_waveform or coverage is enabled (see need_vcd in _run_advanced_inner).
        vcd_for_coverage = tmp / "waveform.vcd"
        cov = _toggle_coverage(vcd_for_coverage)
        if cov:
            result["coverage"] = cov
    return _collect_waveform(tmp, waveform_id, result)


_VERILATOR_ROOT_PROBED = False


def _ensure_verilator_root() -> None:
    """Locate Verilator's data directory for portable/relocatable installs.

    Distro packages compile the data path into the binary, so nothing is needed.
    Portable bundles (e.g. OSS CAD Suite) ship it under ``<root>/share/verilator``
    and abort with "Cannot find verilated_std_waiver.vlt" unless VERILATOR_ROOT
    points there. Probed once, then cached.
    """
    global _VERILATOR_ROOT_PROBED
    if _VERILATOR_ROOT_PROBED or os.environ.get("VERILATOR_ROOT"):
        _VERILATOR_ROOT_PROBED = True
        return
    _VERILATOR_ROOT_PROBED = True

    exe = shutil.which("verilator")
    if not exe:
        return
    marker = Path("include") / "verilated_std_waiver.vlt"
    base = Path(exe).resolve().parent
    for cand in (base.parent / "share" / "verilator",   # <root>/bin/verilator
                 base / "share" / "verilator",          # <root>/verilator
                 base.parent,                           # <root>
                 base):
        try:
            if (cand / marker).exists():
                os.environ["VERILATOR_ROOT"] = str(cand)
                logger.info(f"VERILATOR_ROOT auto-detected: {cand}")
                return
        except OSError:
            continue


def _run_verilator(tmp: Path, sources: list, top: str, defines: list,
                   extra_flags: list, coverage: bool, waveform_id: str,
                   plusargs: list = None) -> dict:
    plusargs = plusargs or []
    if not top:
        return {"success": False, "passed": False, "error": "No top module",
                "details": "Verilator needs a top module. Add a testbench module."}
    if not shutil.which("verilator"):
        return {"success": False, "passed": False, "error": "Verilator not installed",
                "details": "Verilator engine is unavailable on this server. "
                           "Use the Icarus Verilog engine instead."}

    mdir = tmp / "obj_dir"
    cmd = ["verilator", "--binary", "--timing", "--assert", "-Wno-fatal",
           "-j", "2", "--top-module", top, "--Mdir", str(mdir)]
    if coverage:
        cmd.append("--coverage")
    if waveform_id:
        cmd.append("--trace")
    for d in defines:
        if _DEFINE_RE.match(d):
            cmd.append("-D" + d)
    cmd += extra_flags
    cmd += [str(p) for p in sources]

    try:
        comp = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120, cwd=str(tmp))
    except subprocess.TimeoutExpired:
        return {"success": False, "passed": False, "error": "Verilation Timeout",
                "details": "Verilator exceeded 120 seconds."}

    combined = (comp.stdout or "") + (comp.stderr or "")
    if comp.returncode != 0:
        return {"success": False, "passed": False, "error": "Verilation Failed",
                "details": combined[:4000]}

    exes = sorted(mdir.glob(f"V{top}")) or sorted(mdir.glob("V*"))
    if not exes:
        return {"success": False, "passed": False, "error": "Verilator build error",
                "details": "No executable was produced.\n" + combined[:1500]}

    try:
        sim = subprocess.run(
            [str(exes[0])] + ["+" + p for p in plusargs],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp),
            preexec_fn=_set_resource_limits if sys.platform != "win32" else None
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "passed": False, "error": "Simulation Timeout",
                "details": "Verilated binary exceeded 30 seconds."}

    output = sim.stdout + sim.stderr
    passed, message = _detect_pass(output, sim.returncode)
    if "%Error" in output and "assert" in output.lower():
        passed, message = False, "Assertion failed"

    warn_notes = combined.strip()
    display = (warn_notes + "\n" + output) if warn_notes else output
    result = {
        "success": True,
        "passed": passed,
        "output": display[:8000],
        "error": "",
        "details": "",
        "message": message,
        "engine": "verilator",
        "top_module": top,
        "compile_warnings": warn_notes[:2000],
    }
    if coverage:
        cov = _read_verilator_coverage(tmp)
        if cov:
            result["coverage"] = cov
    return _collect_waveform(tmp, waveform_id, result)


class AdvanceFile(BaseModel):
    name: str = "design.v"
    content: str = ""


class AdvancedRunRequest(BaseModel):
    files: list[AdvanceFile] = Field(default_factory=list)
    testbench: str = ""
    top_module: str = ""
    language: str = "verilog"
    engine: str = "iverilog"
    generation: str = "2012"
    defines: list[str] = Field(default_factory=list)
    extra_flags: str = ""
    parameters: list[str] = Field(default_factory=list)   # -P path=value
    plusargs: list[str] = Field(default_factory=list)      # runtime +args
    vvp_flags: list[str] = Field(default_factory=list)     # safe vvp runtime flags
    preprocess_only: bool = False                          # -E
    generate_waveform: bool = False
    coverage: bool = False
    problem_id: str = ""


@app.get("/api/engines")
async def get_engines():
    """Report which simulation engines are available on the server."""
    return {
        "iverilog": bool(shutil.which("iverilog")) and bool(shutil.which("vvp")),
        "verilator": bool(shutil.which("verilator")),
        "yosys": bool(shutil.which("yosys")),
        "generations": list(ALLOWED_GENERATIONS.keys()),
    }


@app.post("/api/run-advanced")
@limiter.limit("20/minute")
async def run_advanced(request: Request, body: AdvancedRunRequest):
    """Full-featured simulator: multi-file, all Icarus generations,
    SystemVerilog, defines/flags, VCD waveform, Verilator assertions + coverage."""
    try:
        # --- basic input guards ---
        if len(body.files) > 20:
            raise HTTPException(status_code=400, detail="Too many files (max 20).")

        total_chars = sum(len(f.content or "") for f in body.files) + len(body.testbench or "")
        if total_chars > 400_000:
            raise HTTPException(status_code=413, detail="Source too large (max 400 KB).")

        extra_flags = _sanitize_flags(body.extra_flags)
        defines = [d.strip() for d in body.defines if d and _DEFINE_RE.match(d.strip())]
        params = [p.strip() for p in body.parameters if p and _PARAM_RE.match(p.strip())]
        plusargs = [a.strip().lstrip("+") for a in body.plusargs
                    if a and _PLUSARG_RE.match(a.strip().lstrip("+"))]
        vvp_flags = [f.strip() for f in body.vvp_flags if f.strip() in _VVP_FLAGS_ALLOWED]

        try:
            await asyncio.wait_for(sim_semaphore.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503,
                                detail="Server busy. Please try again in a moment.")

        try:
            result = _run_advanced_inner(body, defines, extra_flags,
                                         params, plusargs, vvp_flags)
        finally:
            sim_semaphore.release()

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in run_advanced: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _run_advanced_inner(body: AdvancedRunRequest, defines: list, extra_flags: list,
                        params: list = None, plusargs: list = None,
                        vvp_flags: list = None) -> dict:
    params = params or []
    plusargs = plusargs or []
    vvp_flags = vvp_flags or []
    engine = (body.engine or "iverilog").lower()
    generation = body.generation or ("systemverilog" if body.language == "systemverilog" else "2012")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # --- write RTL files ---
        written = []
        used_names = set()
        for i, f in enumerate(body.files):
            fn = _safe_filename(f.name, i)
            while fn in used_names:
                fn = f"{i}_{fn}"
            used_names.add(fn)
            p = tmp / fn
            p.write_text(f.content or "", encoding="utf-8")
            written.append(p)

        # --- write testbench (if supplied separately) ---
        if body.testbench and body.testbench.strip():
            tb_name = "tb_chipversity.sv" if engine == "verilator" or body.language == "systemverilog" else "tb_chipversity.v"
            tb_path = tmp / tb_name
            tb_path.write_text(body.testbench, encoding="utf-8")
            written.append(tb_path)

        if not written:
            return {"success": False, "passed": False, "error": "No source files",
                    "details": "Add at least one Verilog/SystemVerilog file to run."}

        # --- waveform injection ---
        # Icarus toggle-coverage needs a VCD even if the user did not ask for a waveform.
        need_vcd = body.generate_waveform or (body.coverage and engine == "iverilog")
        waveform_id = None
        if need_vcd:
            if body.generate_waveform:
                waveform_id = str(uuid.uuid4())
            vcd_path = str(tmp / "waveform.vcd").replace("\\", "/")
            # inject into the testbench if present, otherwise the last source file
            tb_file = next((p for p in written if p.name.startswith("tb_")), None)
            target = tb_file or written[-1]
            target.write_text(_inject_vcd(target.read_text(encoding="utf-8"), vcd_path),
                              encoding="utf-8")

        # --- resolve top module ---
        src_map = {p.name: p.read_text(encoding="utf-8", errors="ignore") for p in written}
        top = (body.top_module or "").strip()
        if not top or not _NAME_RE.match(top):
            top = detect_top_module(src_map)

        if engine == "verilator":
            return _run_verilator(tmp, written, top, defines, extra_flags,
                                  body.coverage, waveform_id, plusargs)
        return _run_iverilog(tmp, written, top, generation, defines,
                             extra_flags, waveform_id, params, plusargs,
                             vvp_flags, body.preprocess_only, body.coverage)


# ============================================================
# IDE extras — toggle coverage, Verilator lint, Yosys synthesis,
# and shareable projects (ChipVerify-lab style panels)
# ============================================================

def _toggle_coverage(vcd_path: Path):
    """Compute per-signal toggle coverage from a VCD file."""
    if not vcd_path or not vcd_path.exists():
        return None
    try:
        parser = VCDParser(vcd_path)
        if not parser.parse():
            return None
    except Exception as e:
        logger.warning(f"toggle coverage parse failed: {e}")
        return None

    per = []
    toggled = 0
    for sig in parser.signals:
        changes = parser.waveform_data.get(sig["name"], [])
        rose = fell = 0
        prev = None
        for ev in changes:
            v = ev.get("value")
            if prev is not None:
                if prev == "0" and v == "1":
                    rose += 1
                elif prev == "1" and v == "0":
                    fell += 1
            prev = v
        covered = rose > 0 and fell > 0
        if rose or fell:
            toggled += 1
        per.append({
            "name": sig["name"], "width": sig.get("width", "1"),
            "rose": rose, "fell": fell, "covered": covered,
        })

    total = len(per)
    return {
        "type": "toggle",
        "total_signals": total,
        "toggled": toggled,
        "toggle_percent": round(100.0 * toggled / total, 1) if total else 0.0,
        "untoggled": [p["name"] for p in per if not p["rose"] and not p["fell"]][:200],
        "signals": per[:400],
    }


def _materialize_sources(tmp: Path, body_files, testbench: str = "",
                         engine: str = "iverilog", language: str = "verilog") -> list:
    """Write uploaded files (and optional testbench) into the temp workspace."""
    written = []
    used = set()
    for i, f in enumerate(body_files):
        fn = _safe_filename(f.name, i)
        while fn in used:
            fn = f"{i}_{fn}"
        used.add(fn)
        p = tmp / fn
        p.write_text(f.content or "", encoding="utf-8")
        written.append(p)
    if testbench and testbench.strip():
        tb_name = ("tb_chipversity.sv"
                   if (engine == "verilator" or language == "systemverilog")
                   else "tb_chipversity.v")
        p = tmp / tb_name
        p.write_text(testbench, encoding="utf-8")
        written.append(p)
    return written


# The path group must tolerate colons: sources are compiled by absolute path, and
# a Windows path ("C:\...\dirty.v:9:15:") breaks a naive [^:]* match, which left
# file="" and line=0 and disabled click-to-jump in the UI.
_DIAG_RE = re.compile(
    r'^%(Warning|Error)(?:-([A-Z0-9_]+))?:\s*'
    r'(?:(.+?):(\d+):(\d+):\s*)?(.*)$'
)

# "%Error: Exiting due to N error(s)" is a roll-up, not a real diagnostic.
_DIAG_SUMMARY_RE = re.compile(
    r'^%(?:Warning|Error)(?:-[A-Z0-9_]+)?:\s*Exiting due to\b'
)


def _parse_verilator_diags(text: str) -> list:
    """Turn Verilator lint output into structured diagnostics.

    Diagnostics without a file:line prefix (e.g. "Cannot find file containing
    module: 'x'", bad flags) are kept too — they are real errors, and dropping
    them made a hard failure look like a clean pass.
    """
    diags = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        m = _DIAG_RE.match(line)
        if not m or _DIAG_SUMMARY_RE.match(line):
            continue
        f = m.group(3) or ""
        diags.append({
            "severity": "error" if m.group(1) == "Error" else "warning",
            "code": m.group(2) or "",
            "file": os.path.basename(f) if f else "",
            "line": int(m.group(4) or 0),
            "col": int(m.group(5) or 0),
            "message": (m.group(6) or "").strip(),
        })
    return diags


class LintRequest(BaseModel):
    files: list[AdvanceFile] = Field(default_factory=list)
    testbench: str = ""
    include_testbench: bool = False
    top_module: str = ""
    language: str = "verilog"
    generation: str = "2012"
    defines: list[str] = Field(default_factory=list)
    extra_flags: str = ""


def _parse_yosys_stats(text: str) -> dict:
    """Extract cell counts and basic metrics from a Yosys ``stat`` block.

    Yosys has changed this output shape over time and both are in the wild:
      old:  "Number of cells:  24"   then "  $_AND_   8"
      new:  "24 cells"               then "  8   $_AND_"
    Yosys 0.69 uses the new shape, so the old regex alone silently returned
    nothing and the Synthesis panel showed no cell breakdown.
    """
    _METRICS = ("wire bits", "public wire bits", "wires", "public wires",
                "memories", "processes", "ports", "port bits")

    cells = {}
    metrics = {}
    total = None
    mode = None  # None | "old_cells" | "new_cells"

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()

        m = re.match(r'\s*Number of cells:\s*(\d+)\s*$', line)
        if m:
            total, mode = int(m.group(1)), "old_cells"
            continue

        m = re.match(r'\s*(\d+)\s+cells\s*$', line)
        if m:
            total, mode = int(m.group(1)), "new_cells"
            continue

        m = re.match(r'\s*Number of ([a-z ]+?):\s*(\d+)\s*$', line)
        if m and m.group(1) in _METRICS:
            metrics[m.group(1)] = int(m.group(2))
            mode = None
            continue

        m = re.match(r'\s*(\d+)\s+([a-z ]+?)\s*$', line)
        if m and m.group(2) in _METRICS:
            metrics[m.group(2)] = int(m.group(1))
            mode = None
            continue

        if mode == "old_cells":
            m = re.match(r'\s+(\S+)\s+(\d+)\s*$', line)
            if m:
                cells[m.group(1)] = int(m.group(2))
                continue
            if line.strip():
                mode = None
        elif mode == "new_cells":
            m = re.match(r'\s+(\d+)\s+(\S+)\s*$', line)
            if m:
                cells[m.group(2)] = int(m.group(1))
                continue
            if line.strip():
                mode = None

    return {"cell_count": total, "cells": cells, "metrics": metrics}


class SynthRequest(BaseModel):
    files: list[AdvanceFile] = Field(default_factory=list)
    top_module: str = ""
    language: str = "verilog"
    generation: str = "2012"
    defines: list[str] = Field(default_factory=list)


class ShareRequest(BaseModel):
    project: dict = Field(default_factory=dict)


@app.post("/api/lint")
@limiter.limit("20/minute")
async def lint_code(request: Request, body: LintRequest):
    """Run Verilator --lint-only and return structured diagnostics."""
    try:
        if not shutil.which("verilator"):
            return {"success": False, "error": "Verilator not installed",
                    "details": "The Verilator lint engine is unavailable on this server.",
                    "diagnostics": [], "warning_count": 0, "error_count": 0}
        _ensure_verilator_root()

        extra = _sanitize_flags(body.extra_flags)
        defines = [d.strip() for d in body.defines if d and _DEFINE_RE.match(d.strip())]
        is_sv = body.language == "systemverilog" or body.generation in ("systemverilog", "sv")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            written = _materialize_sources(
                tmp, body.files,
                body.testbench if body.include_testbench else "",
                "verilator", body.language)
            if not written:
                return {"success": False, "error": "No source files",
                        "details": "Add at least one design file.", "diagnostics": []}

            src_map = {p.name: p.read_text(encoding="utf-8", errors="ignore") for p in written}
            top = (body.top_module or "").strip()
            if not top or not _NAME_RE.match(top):
                top = detect_top_module(src_map)

            # NOTE: -I must be attached to its directory ("-I<dir>"). Passing
            # "-I <dir>" as two argv entries makes Verilator treat the directory
            # as a *module name* and abort with "Cannot find file containing
            # module: '.'", which silently produced an empty diagnostic list.
            cmd = ["verilator", "--lint-only", "-Wall", "-Wno-fatal",
                   "-I" + str(tmp), "-I."]
            if is_sv:
                cmd.append("-sv")
            if top:
                cmd += ["--top-module", top]
            for d in defines:
                cmd.append("-D" + d)
            cmd += extra
            cmd += [str(p) for p in written]

            try:
                res = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=60, cwd=str(tmp))
            except FileNotFoundError:
                return {"success": False, "error": "Verilator not installed",
                        "diagnostics": [], "warning_count": 0, "error_count": 0}
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Lint Timeout",
                        "details": "Verilator lint exceeded 60 seconds.", "diagnostics": []}

            raw = (res.stdout or "") + (res.stderr or "")
            diags = _parse_verilator_diags(raw)
            errs = sum(1 for d in diags if d["severity"] == "error")
            warns = sum(1 for d in diags if d["severity"] == "warning")

            # A nonzero exit with nothing parseable (missing include dir, bad
            # flag, internal error) must not be reported as a clean pass.
            if res.returncode != 0 and errs == 0:
                tail = [l.strip() for l in raw.splitlines() if l.strip()]
                diags.append({
                    "severity": "error", "code": "", "file": "", "line": 0, "col": 0,
                    "message": (tail[-1] if tail
                                else f"Verilator exited with status {res.returncode}")[:300],
                })
                errs += 1

            return {
                "success": True,
                "passed": errs == 0,
                "diagnostics": diags,
                "warning_count": warns,
                "error_count": errs,
                "top_module": top,
                "raw": raw[:8000],
            }
    except Exception as e:
        logger.error(f"Error in lint_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/synthesize")
@limiter.limit("10/minute")
async def synthesize(request: Request, body: SynthRequest):
    """Synthesize RTL to a gate-level netlist with Yosys (generic cells)."""
    if not shutil.which("yosys"):
        return {"success": False, "error": "Yosys not installed",
                "details": "Synthesis is unavailable on this server. "
                           "Icarus simulation still works.",
                "netlist": "", "stats": {}}

    if not body.files:
        return {"success": False, "error": "No source files",
                "details": "Add at least one design file.", "netlist": "", "stats": {}}

    is_sv = body.language == "systemverilog" or body.generation in ("systemverilog", "sv")
    defines = [d.strip() for d in body.defines if d and _DEFINE_RE.match(d.strip())]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        written = _materialize_sources(tmp, body.files, "", "yosys", body.language)
        src_map = {p.name: p.read_text(encoding="utf-8", errors="ignore") for p in written}
        top = (body.top_module or "").strip()
        if not top or not _NAME_RE.match(top):
            top = detect_top_module(src_map)
        if not top:
            return {"success": False, "error": "No top module",
                    "details": "Could not detect a top module.", "netlist": "", "stats": {}}

        out = tmp / "netlist.v"
        rd = "read_verilog -sv" if is_sv else "read_verilog"
        defs = " ".join("-D" + d for d in defines)
        srcs = " ".join(str(p) for p in written)
        script = (
            f"{rd} {defs} {srcs}; "
            f"hierarchy -top {top}; "
            f"proc; opt; fsm; opt; memory; opt; techmap; opt; "
            f"write_verilog -noattr {out}; stat"
        )
        try:
            res = subprocess.run(["yosys", "-p", script], capture_output=True,
                                 text=True, timeout=120, cwd=str(tmp))
        except FileNotFoundError:
            return {"success": False, "error": "Yosys not installed",
                    "netlist": "", "stats": {}}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Synthesis Timeout",
                    "details": "Yosys exceeded 120 seconds.", "netlist": "", "stats": {}}

        raw = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0 or not out.exists():
            return {"success": False, "error": "Synthesis Failed",
                    "details": raw[-4000:], "netlist": "", "stats": {}}

        return {
            "success": True,
            "engine": "yosys",
            "top_module": top,
            "netlist": out.read_text(encoding="utf-8", errors="ignore")[:120000],
            "stats": _parse_yosys_stats(raw),
            "raw": raw[-4000:],
        }


@app.post("/api/share")
async def create_share(body: ShareRequest):
    """Store a project snapshot and return a short share id."""
    try:
        sid = uuid.uuid4().hex[:10]
        payload = {
            "id": sid,
            "created": datetime.utcnow().isoformat() + "Z",
            "project": body.project or {},
        }
        (SHARE_DIR / f"{sid}.json").write_text(
            json.dumps(payload), encoding="utf-8")
        return {"success": True, "id": sid,
                "url": f"simulator-pro.html#share={sid}"}
    except Exception as e:
        logger.error(f"share create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/share/{sid}")
async def get_share(sid: str):
    """Fetch a shared project snapshot."""
    if not re.match(r'^[a-f0-9]{6,32}$', sid):
        raise HTTPException(status_code=400, detail="Invalid share id")
    path = SHARE_DIR / f"{sid}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Shared project not found")
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
    
