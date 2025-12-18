#!/usr/bin/env python3
"""
Backend API for VLSI Practice with MongoDB Waveform Storage
Clean and simple version with auto-cleanup
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import uuid
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any
from pathlib import Path
import motor.motor_asyncio

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

# MongoDB configuration
MONGODB_URL = os.environ.get("MONGODB_URI", "mongodb+srv://teddugovardhan544_db_user:WVjIA96jQ31net0j@cluster0.kwkkleo.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
DATABASE_NAME = os.environ.get("MONGODB_DB", "Cluster0")

# Initialize MongoDB client
try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
    db = client[DATABASE_NAME]
    waveforms_collection = db["waveforms"]
    logger.info(f"Connected to MongoDB: {DATABASE_NAME}")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    client = None
    waveforms_collection = None

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
    generate_waveform: bool = False

# Load problems
PROBLEMS = []
with open("problems.json", "r") as f:
    PROBLEMS = json.load(f)
logger.info(f"Loaded {len(PROBLEMS)} problems")

@app.get("/")
async def root():
    return {"status": "VLSI Practice API", "version": "3.0"}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    return {"problems": PROBLEMS}

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str, download: bool = False):
    """Serve waveform from MongoDB"""
    try:
        if not waveforms_collection:
            raise HTTPException(status_code=500, detail="Database not available")
        
        # Find waveform in database
        waveform = await waveforms_collection.find_one({"waveform_id": waveform_id})
        
        if not waveform:
            raise HTTPException(status_code=404, detail=f"Waveform {waveform_id} not found")
        
        # Check if expired and clean up
        expires_at = waveform.get("expires_at")
        if expires_at and datetime.now() > expires_at:
            await waveforms_collection.delete_one({"waveform_id": waveform_id})
            raise HTTPException(status_code=404, detail="Waveform expired")
        
        # Return HTML or VCD based on request
        if download:
            vcd_content = waveform.get("vcd_content", "")
            if not vcd_content:
                raise HTTPException(status_code=404, detail="VCD content not available")
            
            return Response(
                content=vcd_content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={waveform_id}.vcd"}
            )
        else:
            html_content = waveform.get("html_content", "")
            if not html_content:
                html_content = create_basic_html(waveform_id)
            
            return HTMLResponse(content=html_content)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run")
async def run_code(request: CodeRequest, background_tasks: BackgroundTasks):
    """Execute Verilog code"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Run simulation
        result = await run_simulation(
            request.code,
            problem["testbench"],
            request.generate_waveform,
            problem["title"]
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
            response["waveform_download_url"] = f"/api/waveform/{waveform_id}?download=true"
            
            # Schedule cleanup after 24 hours
            background_tasks.add_task(schedule_waveform_cleanup, waveform_id)
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def schedule_waveform_cleanup(waveform_id: str):
    """Schedule cleanup of waveform after 24 hours"""
    await asyncio.sleep(24 * 3600)  # 24 hours
    if waveforms_collection:
        await waveforms_collection.delete_one({"waveform_id": waveform_id})
        logger.info(f"Cleaned up expired waveform: {waveform_id}")

async def run_simulation(user_code: str, testbench: str, generate_waveform: bool, problem_title: str) -> Dict:
    """Run Verilog simulation and store result in MongoDB"""
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
            
            # Save waveform to MongoDB if requested
            if generate_waveform and waveform_id and waveforms_collection:
                vcd_file = tmp_path / "waveform.vcd"
                if vcd_file.exists() and vcd_file.stat().st_size > 0:
                    try:
                        vcd_content = vcd_file.read_text()
                        html_content = create_simple_html(waveform_id)
                        
                        waveform_doc = {
                            "waveform_id": waveform_id,
                            "vcd_content": vcd_content,
                            "html_content": html_content,
                            "created_at": datetime.now(),
                            "expires_at": datetime.now() + timedelta(hours=24),
                        }
                        
                        await waveforms_collection.insert_one(waveform_doc)
                        logger.info(f"Waveform saved: {waveform_id}")
                        result["waveform_id"] = waveform_id
                        
                    except Exception as e:
                        logger.error(f"Failed to save waveform: {e}")
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def create_simple_html(waveform_id: str) -> str:
    """Create simple HTML viewer"""
    return f'''<!DOCTYPE html>
<html>
<head>
    <title>Waveform {waveform_id}</title>
    <style>
        body {{ font-family: Arial, sans-serif; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }}
        .info {{ background: #e8f5e9; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .btn {{ display: inline-block; background: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin: 10px 5px; }}
        .btn:hover {{ background: #45a049; }}
        .btn-vcd {{ background: #2196F3; }}
        .btn-vcd:hover {{ background: #1976D2; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Digital Waveform</h1>
        <div class="info">
            <p><strong>ID:</strong> {waveform_id}</p>
            <p><strong>Format:</strong> VCD (Value Change Dump)</p>
            <p><strong>Expires:</strong> 24 hours from generation</p>
        </div>
        
        <a href="/api/waveform/{waveform_id}?download=true" class="btn btn-vcd">
            ⬇ Download VCD File
        </a>
        <a href="/api/waveform/{waveform_id}" class="btn">
            🔄 Refresh
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

def create_basic_html(waveform_id: str) -> str:
    """Create basic HTML fallback"""
    return f'''<!DOCTYPE html>
<html>
<head><title>Waveform {waveform_id}</title></head>
<body style="font-family: Arial; padding: 20px;">
    <h1>Waveform Viewer</h1>
    <p>Waveform ID: <code>{waveform_id}</code></p>
    <p><a href="/api/waveform/{waveform_id}?download=true">Download VCD File</a></p>
</body>
</html>'''

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    mongo_status = "connected" if client else "disconnected"
    
    waveform_count = 0
    if waveforms_collection:
        try:
            waveform_count = await waveforms_collection.count_documents({})
        except:
            pass
    
    return {
        "status": "healthy",
        "mongo": mongo_status,
        "waveforms": waveform_count,
        "problems": len(PROBLEMS)
    }

# Background task to clean old waveforms periodically
@app.on_event("startup")
async def startup_event():
    """Start background cleanup task"""
    asyncio.create_task(periodic_cleanup())

async def periodic_cleanup():
    """Clean up expired waveforms every hour"""
    while True:
        try:
            if waveforms_collection:
                # Delete waveforms older than 24 hours
                cutoff = datetime.now() - timedelta(hours=24)
                result = await waveforms_collection.delete_many({
                    "created_at": {"$lt": cutoff}
                })
                if result.deleted_count > 0:
                    logger.info(f"Cleaned up {result.deleted_count} old waveforms")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        await asyncio.sleep(3600)  # Run every hour

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
