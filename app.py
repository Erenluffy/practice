#!/usr/bin/env python3
"""
Backend API for VLSI Practice - Simple & Professional
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
import subprocess
import tempfile
import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VLSI Practice API")

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
    return {"status": "VLSI Practice API", "version": "4.0"}

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
            "template": problem["template"]
        })
    return {"problems": simplified}

@app.get("/api/waveform/{waveform_id}")
async def get_waveform(waveform_id: str, download: bool = False):
    """Serve waveform with professional HTML viewer"""
    try:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        html_path = WAVEFORM_DIR / f"{waveform_id}.html"
        
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
async def run_code(request: CodeRequest):
    """Execute Verilog code"""
    try:
        # Find problem
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        
        # Run simulation
        result = run_simulation(
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

def run_simulation(user_code: str, testbench: str, generate_waveform: bool, problem_title: str) -> dict:
    """Run Verilog simulation"""
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
            
            # Save waveform if requested
            if generate_waveform and waveform_id:
                vcd_file = tmp_path / "waveform.vcd"
                if vcd_file.exists() and vcd_file.stat().st_size > 0:
                    # Save VCD
                    dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                    import shutil
                    shutil.copy2(vcd_file, dest_vcd)
                    
                    # Parse VCD for preview
                    vcd_content = vcd_file.read_text()
                    parse_vcd_for_preview(waveform_id, vcd_content)
                    
                    logger.info(f"Waveform saved: {waveform_id}")
                    result["waveform_id"] = waveform_id
            
            return result
        else:
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1000]
            }

def parse_vcd_for_preview(waveform_id: str, vcd_content: str):
    """Parse VCD to create a preview JSON"""
    signals = []
    timescale = "1ns"
    
    # Parse VCD
    for line in vcd_content.split('\n'):
        line = line.strip()
        if line.startswith('$timescale'):
            parts = line.split()
            if len(parts) > 1:
                timescale = parts[1]
        elif line.startswith('$var'):
            parts = line.split()
            if len(parts) >= 5:
                signals.append({
                    "id": parts[3],
                    "name": parts[4],
                    "type": parts[1],
                    "width": parts[2]
                })
    
    # Save preview data
    preview_data = {
        "waveform_id": waveform_id,
        "signals": signals[:10],  # First 10 signals
        "timescale": timescale,
        "generated_at": datetime.now().isoformat()
    }
    
    preview_path = WAVEFORM_DIR / f"{waveform_id}_preview.json"
    preview_path.write_text(json.dumps(preview_data, indent=2))

def create_professional_viewer(waveform_id: str, vcd_exists: bool) -> str:
    """Create professional HTML waveform viewer"""
    
    # Try to load preview data
    preview_data = None
    preview_path = WAVEFORM_DIR / f"{waveform_id}_preview.json"
    if preview_path.exists():
        try:
            preview_data = json.loads(preview_path.read_text())
        except:
            pass
    
    signals = preview_data.get("signals", []) if preview_data else []
    
    # Create signal items HTML
    signal_items_html = ""
    if signals:
        for signal in signals[:8]:
            color = f"#{hash(signal['name']) % 0xffffff:06x}"
            signal_items_html += f'''
                    <div class="signal-item">
                        <div class="signal-color" style="background: {color};"></div>
                        <div class="signal-name">{signal['name']}</div>
                        <div class="signal-type">{signal['type']} ({signal['width']}bit)</div>
                    </div>
            '''
    else:
        signal_items_html = '''
                    <div style="text-align: center; padding: 20px; color: var(--gray);">
                        <i class="fas fa-info-circle"></i> No signals detected yet
                    </div>
        '''
    
    # Create waveform display HTML
    if vcd_exists:
        waveform_display_html = f'''
                    <div class="waveform-placeholder">
                        <i class="fas fa-wave-square"></i>
                        <h3>Waveform Visualization</h3>
                        <p>Professional waveform display with interactive controls</p>
                        <button class="btn btn-success" onclick="loadWaveformPreview()">
                            <i class="fas fa-play"></i> Load Preview
                        </button>
                    </div>
        '''
        auto_refresh_js = ""
    else:
        waveform_display_html = '''
                    <div class="waveform-placeholder">
                        <i class="fas fa-spinner fa-spin"></i>
                        <h3>Generating Waveform</h3>
                        <p>Please wait while the waveform is being processed...</p>
                    </div>
        '''
        auto_refresh_js = '''
        setTimeout(() => {
            if (document.querySelector('.waveform-placeholder i.fa-spinner')) {
                refreshViewer();
            }
        }, 3000);
        '''
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Waveform Viewer: {waveform_id}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        :root {{
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --secondary: #10b981;
            --dark: #1f2937;
            --light: #f9fafb;
            --gray: #6b7280;
            --gray-light: #e5e7eb;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }}
        
        body {{
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: var(--dark);
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        .header {{
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }}
        
        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        
        .logo {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        
        .logo-icon {{
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 20px;
        }}
        
        .logo-text h1 {{
            font-size: 24px;
            font-weight: 700;
            color: var(--dark);
        }}
        
        .logo-text p {{
            font-size: 14px;
            color: var(--gray);
        }}
        
        .waveform-id {{
            background: var(--light);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid var(--gray-light);
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 14px;
            color: var(--dark);
        }}
        
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }}
        
        .stat-card {{
            background: var(--light);
            padding: 20px;
            border-radius: 10px;
            border-left: 4px solid var(--primary);
        }}
        
        .stat-card i {{
            font-size: 24px;
            color: var(--primary);
            margin-bottom: 10px;
        }}
        
        .stat-card h3 {{
            font-size: 14px;
            color: var(--gray);
            margin-bottom: 5px;
            font-weight: 600;
        }}
        
        .stat-card .value {{
            font-size: 24px;
            font-weight: 700;
            color: var(--dark);
        }}
        
        .main-content {{
            display: grid;
            grid-template-columns: 1fr 350px;
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .waveform-panel {{
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }}
        
        .waveform-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 2px solid var(--gray-light);
        }}
        
        .waveform-header h2 {{
            font-size: 20px;
            font-weight: 700;
            color: var(--dark);
        }}
        
        .controls {{
            display: flex;
            gap: 10px;
        }}
        
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-family: 'Inter', sans-serif;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s ease;
        }}
        
        .btn-primary {{
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            color: white;
        }}
        
        .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(99, 102, 241, 0.3);
        }}
        
        .btn-secondary {{
            background: var(--light);
            color: var(--dark);
            border: 2px solid var(--gray-light);
        }}
        
        .btn-secondary:hover {{
            background: var(--gray-light);
        }}
        
        .btn-success {{
            background: linear-gradient(135deg, var(--success), #0da271);
            color: white;
        }}
        
        .waveform-display {{
            background: #1e1e1e;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 30px;
            min-height: 300px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            color: white;
        }}
        
        .waveform-placeholder {{
            text-align: center;
            padding: 40px;
        }}
        
        .waveform-placeholder i {{
            font-size: 64px;
            color: #4f46e5;
            margin-bottom: 20px;
        }}
        
        .waveform-placeholder h3 {{
            font-size: 20px;
            margin-bottom: 10px;
            color: #e5e7eb;
        }}
        
        .waveform-placeholder p {{
            color: #9ca3af;
            margin-bottom: 20px;
        }}
        
        .signal-list {{
            background: var(--light);
            border-radius: 8px;
            padding: 20px;
        }}
        
        .signal-list h3 {{
            font-size: 16px;
            margin-bottom: 15px;
            color: var(--dark);
            font-weight: 600;
        }}
        
        .signal-item {{
            display: flex;
            align-items: center;
            padding: 12px;
            background: white;
            border-radius: 6px;
            margin-bottom: 8px;
            border-left: 3px solid var(--primary);
        }}
        
        .signal-color {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 12px;
        }}
        
        .signal-name {{
            font-weight: 600;
            flex: 1;
        }}
        
        .signal-type {{
            background: var(--gray-light);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            color: var(--gray);
        }}
        
        .info-panel {{
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }}
        
        .info-panel h2 {{
            font-size: 18px;
            margin-bottom: 20px;
            color: var(--dark);
            font-weight: 700;
        }}
        
        .info-item {{
            margin-bottom: 20px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--gray-light);
        }}
        
        .info-item:last-child {{
            border-bottom: none;
        }}
        
        .info-label {{
            font-size: 14px;
            color: var(--gray);
            margin-bottom: 5px;
            font-weight: 600;
        }}
        
        .info-value {{
            font-size: 16px;
            color: var(--dark);
            font-weight: 500;
        }}
        
        .action-buttons {{
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 30px;
        }}
        
        .action-buttons .btn {{
            width: 100%;
            justify-content: center;
        }}
        
        .footer {{
            text-align: center;
            padding: 30px;
            color: white;
            font-size: 14px;
        }}
        
        .footer a {{
            color: white;
            text-decoration: none;
            font-weight: 600;
        }}
        
        .footer a:hover {{
            text-decoration: underline;
        }}
        
        @media (max-width: 992px) {{
            .main-content {{
                grid-template-columns: 1fr;
            }}
            
            .stats {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
        
        @media (max-width: 768px) {{
            .header-top {{
                flex-direction: column;
                gap: 20px;
                align-items: flex-start;
            }}
            
            .stats {{
                grid-template-columns: 1fr;
            }}
            
            .controls {{
                flex-wrap: wrap;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-top">
                <div class="logo">
                    <div class="logo-icon">
                        <i class="fas fa-wave-square"></i>
                    </div>
                    <div class="logo-text">
                        <h1>Digital Waveform Viewer</h1>
                        <p>Professional VCD waveform visualization</p>
                    </div>
                </div>
                <div class="waveform-id">
                    <i class="fas fa-fingerprint"></i> {waveform_id}
                </div>
            </div>
            
            <div class="stats">
                <div class="stat-card">
                    <i class="fas fa-microchip"></i>
                    <h3>Format</h3>
                    <div class="value">VCD</div>
                </div>
                <div class="stat-card">
                    <i class="fas fa-signal"></i>
                    <h3>Signals</h3>
                    <div class="value">{len(signals)}</div>
                </div>
                <div class="stat-card">
                    <i class="fas fa-clock"></i>
                    <h3>Time Scale</h3>
                    <div class="value">1ns</div>
                </div>
                <div class="stat-card">
                    <i class="fas fa-database"></i>
                    <h3>Status</h3>
                    <div class="value">{'Available' if vcd_exists else 'Processing'}</div>
                </div>
            </div>
        </div>
        
        <!-- Main Content -->
        <div class="main-content">
            <!-- Waveform Panel -->
            <div class="waveform-panel">
                <div class="waveform-header">
                    <h2><i class="fas fa-wave-square"></i> Waveform Visualization</h2>
                    <div class="controls">
                        <button class="btn btn-primary" onclick="refreshViewer()">
                            <i class="fas fa-sync-alt"></i> Refresh
                        </button>
                        <button class="btn btn-secondary" onclick="takeScreenshot()">
                            <i class="fas fa-camera"></i> Screenshot
                        </button>
                    </div>
                </div>
                
                <div class="waveform-display">
                    {waveform_display_html}
                </div>
                
                <div class="signal-list">
                    <h3><i class="fas fa-list"></i> Detected Signals</h3>
                    {signal_items_html}
                </div>
            </div>
            
            <!-- Info Panel -->
            <div class="info-panel">
                <h2><i class="fas fa-info-circle"></i> Waveform Information</h2>
                
                <div class="info-item">
                    <div class="info-label">Waveform ID</div>
                    <div class="info-value">{waveform_id}</div>
                </div>
                
                <div class="info-item">
                    <div class="info-label">Format</div>
                    <div class="info-value">Value Change Dump (VCD)</div>
                </div>
                
                <div class="info-item">
                    <div class="info-label">Status</div>
                    <div class="info-value">
                        <span style="color: {'var(--success)' if vcd_exists else 'var(--warning)'}; font-weight: 600;">
                            {'✓ Available' if vcd_exists else '⏳ Processing'}
                        </span>
                    </div>
                </div>
                
                <div class="info-item">
                    <div class="info-label">Generated</div>
                    <div class="info-value" id="generatedTime">Just now</div>
                </div>
                
                <div class="info-item">
                    <div class="info-label">File Size</div>
                    <div class="info-value" id="fileSize">Loading...</div>
                </div>
                
                <div class="action-buttons">
                    <button class="btn btn-primary" onclick="downloadVCD()">
                        <i class="fas fa-download"></i> Download VCD File
                    </button>
                    
                    <button class="btn btn-secondary" onclick="copyWaveformId()">
                        <i class="fas fa-copy"></i> Copy Waveform ID
                    </button>
                    
                    <button class="btn btn-secondary" onclick="openInNewTab()">
                        <i class="fas fa-external-link-alt"></i> Open in New Tab
                    </button>
                </div>
            </div>
        </div>
        
        <!-- Footer -->
        <div class="footer">
            <p>© 2024 VLSI Practice Platform • <a href="/">Back to Editor</a> • Waveform ID: {waveform_id}</p>
        </div>
    </div>
    
    <script>
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            document.getElementById('generatedTime').textContent = new Date().toLocaleString();
            updateFileInfo();
        }});
        
        function updateFileInfo() {{
            fetch('/api/waveform/{waveform_id}?download=true')
                .then(response => {{
                    const size = response.headers.get('content-length');
                    if (size) {{
                        document.getElementById('fileSize').textContent = 
                            (size / 1024).toFixed(2) + ' KB';
                    }}
                }})
                .catch(() => {{
                    document.getElementById('fileSize').textContent = 'Unknown';
                }});
        }}
        
        function downloadVCD() {{
            window.open('/api/waveform/{waveform_id}?download=true', '_blank');
        }}
        
        function copyWaveformId() {{
            navigator.clipboard.writeText('{waveform_id}')
                .then(() => alert('Waveform ID copied to clipboard!'))
                .catch(() => alert('Failed to copy'));
        }}
        
        function refreshViewer() {{
            window.location.reload();
        }}
        
        function openInNewTab() {{
            window.open(window.location.href, '_blank');
        }}
        
        function takeScreenshot() {{
            html2canvas(document.querySelector('.container')).then(canvas => {{
                const link = document.createElement('a');
                link.download = 'waveform-{waveform_id}.png';
                link.href = canvas.toDataURL('image/png');
                link.click();
            }});
        }}
        
        function loadWaveformPreview() {{
            alert('Professional waveform visualization would load here with libraries like Wavedrom or custom renderer.');
        }}
        
        // Auto-refresh if still processing
        {auto_refresh_js}
    </script>
    
    <!-- Include html2canvas for screenshots -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
</body>
</html>'''
    
    return html

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "waveforms": len(list(WAVEFORM_DIR.glob("*.vcd"))),
        "problems": len(PROBLEMS)
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
