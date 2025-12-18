#!/usr/bin/env python3
"""
Backend API for VLSI Practice with MongoDB Waveform Storage
Optimized for Render.com deployment
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import uuid
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from pathlib import Path

# MongoDB imports
import motor.motor_asyncio

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

# MongoDB configuration - REMOVE YOUR PASSWORD FROM CODE!
# Get from environment variable instead
MONGODB_URL = os.environ.get("MONGODB_URI", "mongodb+srv://teddugovardhan544_db_user:WVjIA96jQ31net0j@cluster0.kwkkleo.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
DATABASE_NAME = os.environ.get("MONGODB_DB", "Cluster0")
WAVEFORM_COLLECTION = "waveforms"

# Initialize MongoDB client
try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
    db = client[DATABASE_NAME]
    waveforms_collection = db[WAVEFORM_COLLECTION]
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
    return {"status": "VLSI Practice API", "version": "3.0", "storage": "MongoDB"}

@app.get("/api/problems")
async def get_problems():
    """Return list of available problems"""
    simplified_problems = []
    for problem in PROBLEMS:
        simplified = {
            "id": problem["id"],
            "title": problem["title"],
            "description": problem["description"],
            "difficulty": problem["difficulty"],
            "category": problem["category"],
            "template": problem["template"]
        }
        simplified_problems.append(simplified)
    
    return {"problems": simplified_problems}

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
        
        # Check if expired
        expires_at = waveform.get("expires_at")
        if expires_at and datetime.now() > expires_at:
            # Delete expired waveform
            await waveforms_collection.delete_one({"waveform_id": waveform_id})
            raise HTTPException(status_code=404, detail="Waveform expired")
        
        # Return HTML or VCD based on request
        if download:
            # Return VCD file for download
            vcd_content = waveform.get("vcd_content", "")
            if not vcd_content:
                raise HTTPException(status_code=404, detail="VCD content not available")
            
            return Response(
                content=vcd_content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={waveform_id}.vcd"}
            )
        else:
            # Return HTML viewer
            html_content = waveform.get("html_content", "")
            if not html_content:
                # Create simple HTML if not exists
                html_content = create_basic_html(waveform_id)
            
            return HTMLResponse(content=html_content)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run")
async def run_code(request: CodeRequest):
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
        
        return response
        
    except Exception as e:
        logger.error(f"Error in run_code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
                        # Read VCD content
                        vcd_content = vcd_file.read_text()
                        
                        # Create HTML viewer
                        html_content = create_waveform_html(waveform_id, vcd_content[:500])  # First 500 chars for preview
                        
                        # Store in MongoDB
                        waveform_doc = {
                            "waveform_id": waveform_id,
                            "vcd_content": vcd_content,
                            "html_content": html_content,
                            "created_at": datetime.now(),
                            "expires_at": datetime.now() + timedelta(hours=24),  # 24 hour expiry
                            "metadata": {
                                "problem": problem_title,
                                "size_bytes": len(vcd_content),
                                "lines": vcd_content.count('\n'),
                                "type": "VCD"
                            }
                        }
                        
                        await waveforms_collection.insert_one(waveform_doc)
                        logger.info(f"✅ Waveform saved to MongoDB: {waveform_id}")
                        
                        result["waveform_id"] = waveform_id
                        
                    except Exception as e:
                        logger.error(f"Failed to save waveform to MongoDB: {e}")
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def create_waveform_html(waveform_id: str, vcd_preview: str = "") -> str:
    """Create HTML viewer for waveform"""
    
    # Extract signal names from VCD preview
    signals = []
    for line in vcd_preview.split('\n'):
        if '$var' in line:
            parts = line.split()
            if len(parts) >= 5:
                signals.append(parts[4])
    
    signal_list = ', '.join(signals[:5])  # Show first 5 signals
    
    # Create the HTML with proper escaping
    vcd_preview_display = vcd_preview[:500] if vcd_preview else ""
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>Waveform {waveform_id}</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }}
        
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px 30px;
        }}
        
        .header h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 5px;
        }}
        
        .header .subtitle {{
            font-size: 14px;
            opacity: 0.9;
        }}
        
        .content {{
            padding: 30px;
        }}
        
        .info-card {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid #4CAF50;
        }}
        
        .info-row {{
            display: flex;
            margin-bottom: 10px;
        }}
        
        .info-label {{
            font-weight: 600;
            width: 150px;
            color: #495057;
        }}
        
        .info-value {{
            flex: 1;
        }}
        
        .actions {{
            display: flex;
            gap: 15px;
            margin: 30px 0;
            flex-wrap: wrap;
        }}
        
        .btn {{
            padding: 12px 24px;
            background: #4CAF50;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s ease;
        }}
        
        .btn:hover {{
            background: #45a049;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        
        .btn-download {{
            background: #2196F3;
        }}
        
        .btn-download:hover {{
            background: #1976D2;
        }}
        
        .waveform-preview {{
            background: #1e1e1e;
            color: #fff;
            padding: 20px;
            border-radius: 8px;
            margin-top: 20px;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            max-height: 300px;
            overflow-y: auto;
        }}
        
        .signal-badge {{
            display: inline-block;
            background: #667eea;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            margin: 2px;
            font-size: 12px;
        }}
        
        .footer {{
            padding: 20px 30px;
            text-align: center;
            color: #6c757d;
            font-size: 14px;
            border-top: 1px solid #e9ecef;
        }}
        
        @media (max-width: 768px) {{
            .actions {{
                flex-direction: column;
            }}
            
            .btn {{
                width: 100%;
                justify-content: center;
            }}
        }}
    </style>
    
    <!-- Font Awesome for icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><i class="fas fa-wave-square"></i> Digital Waveform Viewer</h1>
            <div class="subtitle">ID: {waveform_id} | Generated: <span id="timestamp"></span></div>
        </div>
        
        <div class="content">
            <div class="info-card">
                <div class="info-row">
                    <div class="info-label">Waveform ID:</div>
                    <div class="info-value"><code>{waveform_id}</code></div>
                </div>
                <div class="info-row">
                    <div class="info-label">Format:</div>
                    <div class="info-value">Value Change Dump (VCD)</div>
                </div>
                <div class="info-row">
                    <div class="info-label">Signals Detected:</div>
                    <div class="info-value">
                        {signal_list if signal_list else 'No signals detected in preview'}
                    </div>
                </div>
                <div class="info-row">
                    <div class="info-label">Storage:</div>
                    <div class="info-value">MongoDB (24-hour retention)</div>
                </div>
            </div>
            
            <div class="actions">
                <a href="/api/waveform/{waveform_id}?download=true" class="btn btn-download">
                    <i class="fas fa-download"></i> Download VCD File
                </a>
                <a href="/api/waveform/{waveform_id}" class="btn">
                    <i class="fas fa-redo"></i> Refresh Viewer
                </a>
            </div>
            
            <h3><i class="fas fa-info-circle"></i> How to Use:</h3>
            <ol style="margin: 15px 0 15px 20px; line-height: 1.6;">
                <li>Download the VCD file using the button above</li>
                <li>Open it with GTKWave (desktop application)</li>
                <li>Or use online VCD viewers like Wavedrom</li>
                <li>For quick viewing, paste the VCD content into online tools</li>
            </ol>'''
    
    # Add VCD preview section if we have content
    if vcd_preview_display:
        html += f'''
            <h3><i class="fas fa-eye"></i> VCD Preview (first 500 chars):</h3>
            <div class="waveform-preview">
                <pre>{vcd_preview_display}</pre>
            </div>'''
    
    html += f'''
            <h3><i class="fas fa-microchip"></i> Recommended Tools:</h3>
            <div style="display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;">
                <span class="signal-badge">GTKWave</span>
                <span class="signal-badge">Sigrok</span>
                <span class="signal-badge">ModelSim</span>
                <span class="signal-badge">Wavedrom</span>
                <span class="signal-badge">EDA Playground</span>
            </div>
        </div>
        
        <div class="footer">
            <p>Waveform stored in MongoDB • Auto-expires in 24 hours • <a href="/api/waveform/{waveform_id}?download=true" style="color: #667eea;">Download VCD</a></p>
        </div>
    </div>
    
    <script>
        document.getElementById('timestamp').textContent = new Date().toLocaleString();
        
        // Auto-refresh if tab is visible
        document.addEventListener('visibilitychange', function() {{
            if (!document.hidden) {{
                window.location.reload();
            }}
        }});
    </script>
</body>
</html>'''
    
    return html

def create_basic_html(waveform_id: str) -> str:
    """Create basic HTML if detailed one fails"""
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
    
    # Count waveforms in database
    waveform_count = 0
    if waveforms_collection:
        try:
            waveform_count = await waveforms_collection.count_documents({})
        except:
            pass
    
    return {
        "status": "healthy",
        "storage": "MongoDB",
        "mongo_status": mongo_status,
        "waveform_count": waveform_count,
        "problems_count": len(PROBLEMS),
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/admin/waveforms")
async def list_waveforms(limit: int = 10):
    """List all waveforms in database (admin)"""
    if not waveforms_collection:
        return {"error": "Database not available"}
    
    waveforms = []
    async for doc in waveforms_collection.find().sort("created_at", -1).limit(limit):
        waveforms.append({
            "id": doc.get("waveform_id"),
            "created": doc.get("created_at"),
            "expires": doc.get("expires_at"),
            "size": doc.get("metadata", {}).get("size_bytes", 0),
            "problem": doc.get("metadata", {}).get("problem", "Unknown")
        })
    
    return {"waveforms": waveforms, "total": await waveforms_collection.count_documents({})}

@app.delete("/api/admin/waveforms/{waveform_id}")
async def delete_waveform(waveform_id: str):
    """Delete a waveform from database"""
    if not waveforms_collection:
        raise HTTPException(status_code=500, detail="Database not available")
    
    result = await waveforms_collection.delete_one({"waveform_id": waveform_id})
    
    if result.deleted_count:
        return {"message": f"Waveform {waveform_id} deleted"}
    else:
        raise HTTPException(status_code=404, detail="Waveform not found")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
