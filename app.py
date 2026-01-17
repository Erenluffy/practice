#!/usr/bin/env python3
"""
VLSI Practice Platform - Enhanced Version
Combines authentication, progress tracking, and enhanced waveform viewer
"""

from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, validator, Field
import subprocess
import tempfile
import os
import base64
import json
import uuid
import logging
import requests  # Add this line for GitHub OAuth
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Union
import hashlib
import secrets
from enum import Enum
import zipfile
import io
import asyncio
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
# Try Firebase import (optional)

try:
    from firebase_config import db, FIREBASE_AVAILABLE
    print(f"✅ Firebase status: {FIREBASE_AVAILABLE}")
except ImportError as e:
    db = None
    FIREBASE_AVAILABLE = False
    print(f"ℹ️ Firebase not imported: {e}")
# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="VLSI Practice Platform",
    description="Interactive Verilog learning with authentication, progress tracking, and waveform visualization",
    version="3.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# Security
security = HTTPBearer(auto_error=False)

# Directories
WAVEFORM_DIR = Path("/tmp/waveforms")
WAVEFORM_DIR.mkdir(exist_ok=True, parents=True)
PROBLEMS_CACHE_DIR = Path("/tmp/problems_cache")
PROBLEMS_CACHE_DIR.mkdir(exist_ok=True)
USER_SESSIONS = {}  # In-memory session store (use Redis in production)

# ==================== MODELS ====================
class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXPERT = "expert"

class Category(str, Enum):
    COMBINATIONAL = "combinational"
    SEQUENTIAL = "sequential"
    FSM = "fsm"
    ARITHMETIC = "arithmetic"
    MEMORY = "memory"
    ADVANCED = "advanced"

class User(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=6, description="Password (min 6 chars)")
    name: Optional[str] = Field(None, description="Display name")
    
    @validator('email')
    def validate_email(cls, v):
        if '@' not in v:
            raise ValueError('Invalid email format')
        return v.lower()

class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool = False

class CodeRequest(BaseModel):
    problem_id: str
    code: str
    user_id: Optional[str] = None
    generate_waveform: bool = False
    session_token: Optional[str] = None

class SubmitRequest(BaseModel):
    problem_id: str
    code: str
    user_id: Optional[str] = None
    session_token: Optional[str] = None

class WaveformRequest(BaseModel):
    waveform_id: str
    format: str = "html"  # html, json, vcd
    signals: Optional[List[str]] = None

class UserProgress(BaseModel):
    user_id: str
    problem_id: str
    code: Optional[str] = None
    passed: bool
    timestamp: datetime
    execution_time: Optional[float] = None
    attempts: int = 1

class LeaderboardEntry(BaseModel):
    user_id: str
    name: str
    score: int
    solved: int
    rank: int

# ==================== AUTH & SESSION MANAGEMENT ====================
def create_session_token(user_id: str, remember_me: bool = False) -> str:
    """Create a secure session token"""
    token = secrets.token_urlsafe(32)
    expiry_hours = 720 if remember_me else 24  # 30 days or 1 day
    expiry = datetime.now() + timedelta(hours=expiry_hours)
    
    USER_SESSIONS[token] = {
        "user_id": user_id,
        "created": datetime.now(),
        "expiry": expiry,
        "last_activity": datetime.now()
    }
    return token

def validate_session_token(token: str) -> Optional[Dict]:
    """Validate session token and return user data"""
    if token not in USER_SESSIONS:
        return None
    
    session = USER_SESSIONS[token]
    
    # Check expiry
    if datetime.now() > session["expiry"]:
        del USER_SESSIONS[token]
        return None
    
    # Update last activity
    session["last_activity"] = datetime.now()
    return session

def get_password_hash(password: str) -> str:
    """Hash password using SHA-256 (use bcrypt in production)"""
    salt = secrets.token_hex(16)
    return f"{salt}${hashlib.sha256((salt + password).encode()).hexdigest()}"

def verify_password(stored_hash: str, password: str) -> bool:
    """Verify password against stored hash"""
    if '$' not in stored_hash:
        return stored_hash == hashlib.sha256(password.encode()).hexdigest()
    
    salt, hash_value = stored_hash.split('$')
    return hash_value == hashlib.sha256((salt + password).encode()).hexdigest()

def create_user_id(email: str) -> str:
    """Create deterministic user ID from email"""
    return hashlib.sha256(email.encode()).hexdigest()[:20]
# ==================== OAUTH ENDPOINTS ====================

class OAuthRequest(BaseModel):
    provider: str  # google, github, etc.
    id_token: Optional[str] = None
    access_token: Optional[str] = None
    code: Optional[str] = None  # For OAuth code flow

# At the top of app.py, add these imports


# Update the oauth_login function
@app.post("/api/auth/oauth/login", response_model=Dict)
async def oauth_login(oauth_data: OAuthRequest):
    """Handle OAuth login from Google"""
    try:
        # Only support Google for now
        if oauth_data.provider != "google":
            raise HTTPException(
                status_code=400,
                detail="Only Google OAuth is supported"
            )
        
        # Get Google Client ID from environment
        GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "11871755691-4lp51g2ifrlbm2d6vkqlkbopu1085c6g.apps.googleusercontent.com")
        
        # Try to verify the token as an ID token first
        id_token_to_verify = oauth_data.id_token
        
        try:
            # Verify Google ID token
            idinfo = id_token.verify_oauth2_token(
                id_token_to_verify,
                google_requests.Request(),
                GOOGLE_CLIENT_ID,
                clock_skew_in_seconds=60  # Allow 1 minute clock skew
            )
            
            # Get user info from verified token
            user_id = f"google_{idinfo['sub']}"
            email = idinfo['email']
            name = idinfo.get('name', email.split('@')[0])
            
            logger.info(f"Google login successful for: {email}")
            
        except Exception as verify_error:
            logger.warning(f"ID token verification failed: {verify_error}")
            
            # If ID token verification fails, check if it's an access token
            if oauth_data.access_token:
                try:
                    # Try to get user info using access token
                    userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
                    headers = {"Authorization": f"Bearer {oauth_data.access_token}"}
                    
                    import requests
                    response = requests.get(userinfo_url, headers=headers, timeout=10)
                    
                    if response.status_code == 200:
                        userinfo = response.json()
                        user_id = f"google_{userinfo['sub']}"
                        email = userinfo['email']
                        name = userinfo.get('name', email.split('@')[0])
                        logger.info(f"Got user info via access token for: {email}")
                    else:
                        raise HTTPException(
                            status_code=401,
                            detail="Invalid Google token"
                        )
                except Exception as access_error:
                    logger.error(f"Access token validation failed: {access_error}")
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid Google credentials"
                    )
            else:
                # Check if it's a demo token
                if id_token_to_verify and id_token_to_verify.startswith("demo.") and id_token_to_verify.endswith(".demo"):
                    try:
                        demo_token = id_token_to_verify[5:-5]
                        # Add padding if needed
                        demo_token += '=' * (4 - len(demo_token) % 4)
                        token_data = json.loads(base64.b64decode(demo_token).decode('utf-8'))
                        
                        user_id = f"google_{token_data.get('sub', str(uuid.uuid4())[:12])}"
                        email = token_data.get('email', f"user{secrets.token_hex(6)}@example.com")
                        name = token_data.get('name', email.split('@')[0])
                        
                        logger.info(f"Demo login for: {email}")
                    except Exception as demo_error:
                        logger.error(f"Demo token decode failed: {demo_error}")
                        raise HTTPException(
                            status_code=401,
                            detail="Invalid demo token"
                        )
                else:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid Google token"
                    )
        
        # Create or find user
        session_token = create_session_token(user_id)
        
        # Store user in memory
        user_data = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "session_token": session_token,
            "total_points": 0,
            "solved_problems": [],
            "settings": {},
            "auth_provider": "google"
        }
        
        USER_SESSIONS[session_token] = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "created": datetime.now(),
            "expiry": datetime.now() + timedelta(hours=24)
        }
        
        # Also store in memory user store
        USER_SESSIONS[f"user_{user_id}"] = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "password_hash": None,  # No password for OAuth users
            "created_at": datetime.utcnow().isoformat(),
            "last_login": datetime.utcnow().isoformat(),
            "progress": [],
            "solved_problems": [],
            "total_points": 0,
            "role": "user",
            "auth_provider": "google",
            "settings": {
                "theme": "dark",
                "auto_save": True,
                "waveform_auto_open": True
            }
        }
        
        return {
            "success": True,
            "message": "Google login successful",
            "data": user_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth login failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )
@app.get("/api/auth/oauth/providers", response_model=Dict)
async def get_oauth_providers():
    """Get list of available OAuth providers and their config"""
    try:
        providers = []
        
        # Add Google provider
        google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        google_enabled = bool(google_client_id) or True  # Enable even without config for demo
        
        providers.append({
            "id": "google",
            "name": "Google",
            "icon": "fab fa-google",
            "color": "#DB4437",
            "auth_url": "/api/auth/oauth/google",
            "enabled": google_enabled,
            "client_id": google_client_id if google_client_id else "demo-mode"
        })
        
        # Optional: Add GitHub if you want to enable it later
        # github_client_id = os.environ.get("GITHUB_CLIENT_ID", "")
        # if github_client_id:
        #     providers.append({
        #         "id": "github",
        #         "name": "GitHub",
        #         "icon": "fab fa-github",
        #         "color": "#333333",
        #         "auth_url": "/api/auth/oauth/github",
        #         "enabled": True,
        #         "client_id": github_client_id
        #     })
        
        return {
            "success": True,
            "providers": providers,
            "message": "Available OAuth providers"
        }
        
    except Exception as e:
        logger.error(f"Failed to get OAuth providers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get OAuth providers"
        )
# ==================== PROBLEM MANAGEMENT ====================
def load_problems() -> List[Dict]:
    """Load problems from JSON file with caching"""
    cache_file = PROBLEMS_CACHE_DIR / "problems.json"
    
    try:
        # Try cache first
        if cache_file.exists():
            cache_age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
            if cache_age < timedelta(minutes=5):  # 5 minute cache
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        
        # Load from source
        with open("problems.json", "r", encoding="utf-8") as f:
            content = f.read()
            content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')
            problems = json.loads(content)
        
        # Validate and enhance problems
        enhanced_problems = []
        for i, problem in enumerate(problems):
            # Ensure all required fields
            problem.setdefault("id", f"prob_{i:03d}")
            problem.setdefault("title", f"Problem {i+1}")
            problem.setdefault("difficulty", Difficulty.MEDIUM)
            problem.setdefault("category", Category.COMBINATIONAL)
            problem.setdefault("points", 10)
            problem.setdefault("hint", "")
            problem.setdefault("solution", "")
            problem.setdefault("explanation", "")
            
            # Add tags based on content
            tags = set()
            if "clock" in problem.get("testbench", "").lower():
                tags.add("sequential")
            if "always" in problem.get("template", "").lower():
                tags.add("always-block")
            if "assign" in problem.get("template", "").lower():
                tags.add("assign")
            if "module" in problem.get("template", "").lower():
                tags.add("module")
            
            problem["tags"] = list(tags)
            enhanced_problems.append(problem)
        
        # Save to cache
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(enhanced_problems, f, indent=2)
        
        logger.info(f"Loaded {len(enhanced_problems)} problems")
        return enhanced_problems
        
    except Exception as e:
        logger.error(f"Error loading problems: {e}")
        return []

PROBLEMS = load_problems()

# ==================== CORS & MIDDLEWARE ====================
app.add_middleware(
    CORSMiddleware,
    ALLOW_ORIGINS = [
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://your-frontend-domain.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ==================== API ENDPOINTS ====================

@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint with API info"""
    return {
        "api": "VLSI Practice Platform",
        "version": "3.0.0",
        "endpoints": {
            "auth": "/api/auth/login, /api/auth/register, /api/auth/profile",
            "problems": "/api/problems, /api/problems/{id}",
            "code": "/api/run, /api/submit",
            "waveforms": "/api/waveform/{id}",
            "progress": "/api/progress/{user_id}, /api/leaderboard",
            "admin": "/api/admin/stats (if authenticated)"
        },
        "status": "operational",
        "problems_count": len(PROBLEMS),
        "auth_enabled": FIREBASE_AVAILABLE,
        "timestamp": datetime.now().isoformat()
    }

# ==================== AUTH ENDPOINTS ====================
@app.post("/api/auth/register", response_model=Dict)
async def register(user_data: User):
    """Register a new user"""
    try:
        user_id = create_user_id(user_data.email)
        
        # Check if user exists
        user_exists = False
        if FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            user_exists = user_doc.exists
        
        # Check in-memory sessions (for demo)
        if not user_exists:
            for session in USER_SESSIONS.values():
                if session.get("email") == user_data.email:
                    user_exists = True
                    break
        
        if user_exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this email already exists"
            )
        
        # Create user object
        user_obj = {
            "email": user_data.email,
            "password_hash": get_password_hash(user_data.password),
            "name": user_data.name or user_data.email.split("@")[0],
            "created_at": datetime.utcnow().isoformat(),
            "last_login": None,
            "progress": [],
            "solved_problems": [],
            "total_points": 0,
            "role": "user",
            "settings": {
                "theme": "dark",
                "auto_save": True,
                "waveform_auto_open": True
            }
        }
        
        # Save to Firebase if available
        if FIREBASE_AVAILABLE:
            db.collection("users").document(user_id).set(user_obj)
            logger.info(f"User registered in Firebase: {user_data.email}")
        else:
            # Store in memory for demo
            user_obj["id"] = user_id
            USER_SESSIONS[f"user_{user_id}"] = user_obj
        
        # Create session token
        session_token = create_session_token(user_id)
        
        return {
            "success": True,
            "message": "Registration successful",
            "data": {
                "user_id": user_id,
                "email": user_data.email,
                "name": user_obj["name"],
                "session_token": session_token,
                "created_at": user_obj["created_at"]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )

@app.post("/api/auth/login", response_model=Dict)
async def login(login_data: LoginRequest):
    """Login user and create session"""
    try:
        user_id = create_user_id(login_data.email)
        user_data = None
        
        # Try Firebase first
        if FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
        
        # Try in-memory (for demo)
        if not user_data:
            user_key = f"user_{user_id}"
            if user_key in USER_SESSIONS:
                user_data = USER_SESSIONS[user_key]
        
        # Auto-register if not found (demo feature)
        if not user_data:
            if os.environ.get("ALLOW_AUTO_REGISTER", "true").lower() == "true":
                user_data = {
                    "email": login_data.email,
                    "password_hash": get_password_hash(login_data.password),
                    "name": login_data.email.split("@")[0],
                    "created_at": datetime.utcnow().isoformat(),
                    "progress": [],
                    "solved_problems": [],
                    "total_points": 0,
                    "role": "user",
                    "settings": {
                        "theme": "dark",
                        "auto_save": True,
                        "waveform_auto_open": True
                    }
                }
                
                if FIREBASE_AVAILABLE:
                    db.collection("users").document(user_id).set(user_data)
                else:
                    USER_SESSIONS[f"user_{user_id}"] = user_data
                
                logger.info(f"Auto-registered user: {login_data.email}")
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found. Please register first."
                )
        
        # Verify password
        if not verify_password(user_data["password_hash"], login_data.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Update last login
        user_data["last_login"] = datetime.utcnow().isoformat()
        if FIREBASE_AVAILABLE:
            db.collection("users").document(user_id).update({"last_login": user_data["last_login"]})
        
        # Create session
        session_token = create_session_token(user_id, login_data.remember_me)
        
        return {
            "success": True,
            "message": "Login successful",
            "data": {
                "user_id": user_id,
                "email": user_data["email"],
                "name": user_data.get("name"),
                "session_token": session_token,
                "total_points": user_data.get("total_points", 0),
                "solved_problems": len(user_data.get("solved_problems", [])),
                "settings": user_data.get("settings", {})
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@app.post("/api/auth/logout", response_model=Dict)
async def logout(session_token: str):
    """Logout user by invalidating session"""
    try:
        if session_token in USER_SESSIONS:
            del USER_SESSIONS[session_token]
        
        return {
            "success": True,
            "message": "Logout successful"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Logout failed: {str(e)}"
        )

@app.get("/api/auth/profile/{user_id}", response_model=Dict)
async def get_profile(user_id: str, token: Optional[str] = None):
    """Get user profile"""
    try:
        user_data = None
        
        # Get from Firebase
        if FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
        
        # Get from memory (demo)
        if not user_data:
            user_key = f"user_{user_id}"
            if user_key in USER_SESSIONS:
                user_data = USER_SESSIONS[user_key]
        
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Calculate stats
        progress = user_data.get("progress", [])
        solved_problems = user_data.get("solved_problems", [])
        total_points = user_data.get("total_points", 0)
        
        # Get problem difficulties solved
        difficulty_stats = {"easy": 0, "medium": 0, "hard": 0, "expert": 0}
        for problem_id in solved_problems:
            problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
            if problem:
                diff = problem.get("difficulty", "medium")
                difficulty_stats[diff] = difficulty_stats.get(diff, 0) + 1
        
        # Calculate rank (simplified)
        total_users = len(USER_SESSIONS) // 2  # Rough estimate
        rank = max(1, total_users - len(solved_problems) // 10)
        
        return {
            "user_id": user_id,
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "created_at": user_data.get("created_at"),
            "last_login": user_data.get("last_login"),
            "stats": {
                "solved_problems": len(solved_problems),
                "total_problems": len(PROBLEMS),
                "completion_rate": round(len(solved_problems) / len(PROBLEMS) * 100, 1) if PROBLEMS else 0,
                "total_points": total_points,
                "average_points": round(total_points / len(solved_problems), 1) if solved_problems else 0,
                "rank": rank,
                "difficulty_stats": difficulty_stats
            },
            "recent_activity": progress[-10:] if progress else [],
            "settings": user_data.get("settings", {})
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get profile: {str(e)}"
        )

# ==================== PROBLEM ENDPOINTS ====================
@app.get("/api/problems", response_model=Dict)
async def get_problems(
    user_id: Optional[str] = None,
    difficulty: Optional[Difficulty] = None,
    category: Optional[Category] = None,
    solved_only: bool = False,
    unsolved_only: bool = False,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
):
    """Get problems with filters and user progress"""
    try:
        # Get user progress if user_id provided
        solved_problems = []
        if user_id and FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
                solved_problems = user_data.get("solved_problems", [])
        
        # Filter problems
        filtered_problems = []
        for problem in PROBLEMS:
            # Apply filters
            if difficulty and problem.get("difficulty") != difficulty:
                continue
            if category and problem.get("category") != category:
                continue
            if search and search.lower() not in problem.get("title", "").lower() + problem.get("description", "").lower():
                continue
            if solved_only and problem["id"] not in solved_problems:
                continue
            if unsolved_only and problem["id"] in solved_problems:
                continue
            
            # Format problem response
            formatted_problem = {
                "id": problem["id"],
                "title": problem["title"],
                "description": problem["description"],
                "difficulty": problem.get("difficulty", "medium"),
                "category": problem.get("category", "combinational"),
                "points": problem.get("points", 10),
                "tags": problem.get("tags", []),
                "template": problem.get("template", ""),
                "hint": problem.get("hint", ""),
                "solved": problem["id"] in solved_problems,
                "solution_available": bool(problem.get("solution")),
                "completion_rate": 0  # Would need tracking
            }
            filtered_problems.append(formatted_problem)
        
        # Paginate
        total = len(filtered_problems)
        paginated_problems = filtered_problems[offset:offset + limit]
        
        # Calculate difficulty distribution
        difficulty_dist = {"easy": 0, "medium": 0, "hard": 0, "expert": 0}
        for prob in PROBLEMS:
            diff = prob.get("difficulty", "medium")
            difficulty_dist[diff] = difficulty_dist.get(diff, 0) + 1
        
        return {
            "success": True,
            "problems": paginated_problems,
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total
            },
            "stats": {
                "total_problems": len(PROBLEMS),
                "filtered_problems": total,
                "difficulty_distribution": difficulty_dist,
                "user_solved": len(solved_problems) if user_id else 0
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get problems: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get problems: {str(e)}"
        )

@app.get("/api/problems/{problem_id}", response_model=Dict)
async def get_problem(problem_id: str, user_id: Optional[str] = None):
    """Get specific problem details"""
    try:
        problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
        if not problem:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Problem not found"
            )
        
        # Check if user has solved it
        solved = False
        user_solution = None
        if user_id and FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
                solved = problem_id in user_data.get("solved_problems", [])
                # Get user's solution if exists
                user_solution = user_data.get("solutions", {}).get(problem_id)
        
        # Get related problems
        related_problems = []
        same_category = [p for p in PROBLEMS if p["category"] == problem["category"] and p["id"] != problem_id]
        for rel_prob in same_category[:3]:  # Top 3 related
            related_problems.append({
                "id": rel_prob["id"],
                "title": rel_prob["title"],
                "difficulty": rel_prob.get("difficulty", "medium"),
                "solved": rel_prob["id"] in (user_data.get("solved_problems", []) if user_id and user_data else [])
            })
        
        # Prepare test cases
        test_cases = []
        testbench = problem.get("testbench", "")
        if "test" in testbench:
            # Extract test cases from testbench (simplified)
            lines = testbench.split('\n')
            for line in lines:
                if line.strip().startswith("// Test:"):
                    test_cases.append(line.strip()[7:].strip())
        
        return {
            "success": True,
            "problem": {
                "id": problem["id"],
                "title": problem["title"],
                "description": problem["description"],
                "difficulty": problem.get("difficulty", "medium"),
                "category": problem.get("category", "combinational"),
                "points": problem.get("points", 10),
                "template": problem.get("template", ""),
                "hint": problem.get("hint", ""),
                "test_cases": test_cases[:5],  # Limit to 5 test cases
                "solved": solved,
                "user_solution": user_solution
            },
            "related_problems": related_problems,
            "navigation": {
                "prev": None,  # Could implement
                "next": None   # Could implement
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get problem: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get problem: {str(e)}"
        )

@app.get("/api/problems/{problem_id}/solution", response_model=Dict)
async def get_solution(problem_id: str, user_id: Optional[str] = None):
    """Get problem solution (only if user has solved it)"""
    try:
        # Check if user has solved it
        if user_id and FIREBASE_AVAILABLE:
            user_doc = db.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
                if problem_id not in user_data.get("solved_problems", []):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You must solve the problem first to view the solution"
                    )
        
        problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
        if not problem:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Problem not found"
            )
        
        solution = problem.get("solution", "")
        explanation = problem.get("explanation", "")
        
        return {
            "success": True,
            "solution": solution,
            "explanation": explanation,
            "tips": problem.get("hint", "").split('. ') if problem.get("hint") else []
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get solution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get solution: {str(e)}"
        )

# ==================== CODE EXECUTION ENDPOINTS ====================
@app.post("/api/run", response_model=Dict)
async def run_code(request: CodeRequest, background_tasks: BackgroundTasks = None):
    """Run Verilog code for testing"""
    try:
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Problem not found"
            )
        
        # Run simulation
        start_time = datetime.now()
        result = run_simulation(
            request.code,
            problem.get("testbench", ""),
            request.generate_waveform,
            problem.get("title", "Unknown")
        )
        execution_time = (datetime.now() - start_time).total_seconds()
        
        # Prepare response
        response = {
            "success": result["success"],
            "problem_id": request.problem_id,
            "problem_title": problem.get("title"),
            "execution_time": execution_time,
            "output": result.get("output", "")[:2000],  # Limit output size
            "error": result.get("error", ""),
            "details": result.get("details", ""),
            "timestamp": datetime.now().isoformat()
        }
        
        if not result["success"] and problem.get("hint"):
            response["hint"] = problem["hint"]
        
        if "waveform_id" in result:
            waveform_id = result["waveform_id"]
            response["waveform"] = {
                "id": waveform_id,
                "url": f"/api/waveform/{waveform_id}",
                "download_url": f"/api/waveform/{waveform_id}?download=true",
                "preview_url": f"/api/waveform/{waveform_id}/preview"
            }
        
        # Store attempt if user is logged in
        if request.user_id and FIREBASE_AVAILABLE:
            background_tasks.add_task(
                store_attempt,
                request.user_id,
                request.problem_id,
                request.code,
                result["success"],
                execution_time
            )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Simulation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Simulation failed: {str(e)}"
        )

@app.post("/api/submit", response_model=Dict)
async def submit_solution(request: SubmitRequest, background_tasks: BackgroundTasks = None):
    """Submit solution for grading and progress tracking"""
    try:
        problem = next((p for p in PROBLEMS if p["id"] == request.problem_id), None)
        if not problem:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Problem not found"
            )
        
        # Run simulation
        start_time = datetime.now()
        result = run_simulation(
            request.code,
            problem.get("testbench", ""),
            generate_waveform=False,
            problem_title=problem.get("title", "")
        )
        execution_time = (datetime.now() - start_time).total_seconds()
        
        response = {
            "success": result["success"],
            "correct": result["success"],
            "execution_time": execution_time,
            "message": "",
            "next_problem": None,
            "achievements": []
        }
        
        if result["success"]:
            response["message"] = "🎉 Correct! Well done!"
            
            # Check if this is the first correct submission
            if request.user_id:
                first_time_solved = False
                
                if FIREBASE_AVAILABLE:
                    user_doc_ref = db.collection("users").document(request.user_id)
                    user_doc = user_doc_ref.get()
                    
                    if user_doc.exists:
                        user_data = user_doc.to_dict()
                        solved_problems = user_data.get("solved_problems", [])
                        
                        if request.problem_id not in solved_problems:
                            first_time_solved = True
                            solved_problems.append(request.problem_id)
                            
                            # Update user progress
                            updates = {
                                "solved_problems": solved_problems,
                                "total_points": user_data.get("total_points", 0) + problem.get("points", 10),
                                f"solutions.{request.problem_id}": {
                                    "code": request.code,
                                    "submitted_at": datetime.utcnow().isoformat(),
                                    "execution_time": execution_time,
                                    "attempts": user_data.get("attempts", {}).get(request.problem_id, 0) + 1
                                }
                            }
                            user_doc_ref.update(updates)
                            
                            # Check for achievements
                            achievements = check_achievements(user_data, solved_problems)
                            if achievements:
                                response["achievements"] = achievements
                            
                            response["message"] += f" +{problem.get('points', 10)} points!"
                        else:
                            response["message"] = "✅ Already solved! Good reinforcement!"
                else:
                    # Demo mode
                    response["message"] += " (Demo mode - progress not saved)"
                
                # Find next recommended problem
                next_prob = get_next_recommended_problem(request.user_id, request.problem_id)
                if next_prob:
                    response["next_problem"] = next_prob
        
        else:
            response["message"] = "❌ Incorrect solution. Try again!"
            if problem.get("hint"):
                response["hint"] = problem["hint"]
        
        # Store attempt
        if request.user_id and FIREBASE_AVAILABLE:
            background_tasks.add_task(
                store_attempt,
                request.user_id,
                request.problem_id,
                request.code,
                result["success"],
                execution_time
            )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Submission failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Submission failed: {str(e)}"
        )

# ==================== WAVEFORM ENDPOINTS ====================
@app.get("/api/waveform/{waveform_id}")
async def get_waveform(
    waveform_id: str,
    download: bool = False,
    format: str = "html",
    signals: Optional[str] = None
):
    """Serve waveform in various formats"""
    try:
        vcd_path = WAVEFORM_DIR / f"{waveform_id}.vcd"
        
        if not vcd_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Waveform not found"
            )
        
        if download:
            return FileResponse(
                vcd_path,
                media_type="application/octet-stream",
                filename=f"{waveform_id}.vcd"
            )
        
        if format == "json":
            # Parse VCD and return as JSON
            parser = VCDParser(vcd_path)
            if parser.parse():
                signal_list = signals.split(',') if signals else None
                waveform_data = parser.get_waveform_summary(signal_list)
                return {
                    "success": True,
                    "waveform_id": waveform_id,
                    "signals": parser.signals,
                    "timescale": parser.timescale,
                    "max_time": parser.max_time,
                    "data": waveform_data
                }
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to parse VCD file"
                )
        
        elif format == "preview":
            # Return a simplified preview
            return HTMLResponse(content=create_waveform_preview(waveform_id, vcd_path.exists()))
        
        else:
            # Return full HTML viewer
            return HTMLResponse(content=create_professional_viewer(waveform_id, vcd_path.exists()))
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving waveform: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error serving waveform: {str(e)}"
        )

@app.get("/api/waveform/{waveform_id}/thumbnail")
async def get_waveform_thumbnail(waveform_id: str):
    """Generate waveform thumbnail (placeholder)"""
    # In production, generate actual thumbnail using pyvcd or similar
    return {"message": "Thumbnail generation not implemented"}

# ==================== PROGRESS & LEADERBOARD ====================
@app.get("/api/progress/{user_id}", response_model=Dict)
async def get_progress(user_id: str):
    """Get user progress and statistics"""
    try:
        if not FIREBASE_AVAILABLE:
            return {
                "user_id": user_id,
                "message": "Firebase not configured - using demo data",
                "stats": {
                    "solved_problems": 0,
                    "total_problems": len(PROBLEMS),
                    "completion_rate": 0,
                    "total_points": 0,
                    "rank": "N/A"
                },
                "recent_activity": []
            }
        
        user_doc = db.collection("users").document(user_id).get()
        if not user_doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        user_data = user_doc.to_dict()
        solved_problems = user_data.get("solved_problems", [])
        total_points = user_data.get("total_points", 0)
        
        # Calculate completion by difficulty
        difficulty_stats = {}
        for problem_id in solved_problems:
            problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
            if problem:
                diff = problem.get("difficulty", "medium")
                difficulty_stats[diff] = difficulty_stats.get(diff, 0) + 1
        
        # Get recent attempts
        recent_attempts = user_data.get("attempts", {})
        recent_activity = []
        for problem_id, attempts in list(recent_attempts.items())[-10:]:
            problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
            if problem:
                recent_activity.append({
                    "problem_id": problem_id,
                    "title": problem["title"],
                    "last_attempt": attempts.get("timestamp"),
                    "solved": problem_id in solved_problems
                })
        
        return {
            "user_id": user_id,
            "stats": {
                "solved_problems": len(solved_problems),
                "total_problems": len(PROBLEMS),
                "completion_rate": round(len(solved_problems) / len(PROBLEMS) * 100, 1) if PROBLEMS else 0,
                "total_points": total_points,
                "average_points": round(total_points / len(solved_problems), 1) if solved_problems else 0,
                "difficulty_stats": difficulty_stats,
                "streak_days": calculate_streak(user_data.get("login_streak", {}))
            },
            "recent_activity": recent_activity,
            "solved_problems": solved_problems[:20]  # First 20
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get progress: {str(e)}"
        )

@app.get("/api/leaderboard", response_model=Dict)
async def get_leaderboard(
    limit: int = 50,
    offset: int = 0,
    timeframe: str = "all"  # all, weekly, monthly
):
    """Get leaderboard rankings"""
    try:
        # In production, this would query Firebase
        # For demo, create mock data
        leaderboard = []
        
        if FIREBASE_AVAILABLE:
            users_ref = db.collection("users")
            users = users_ref.order_by("total_points", direction="DESCENDING").limit(limit).offset(offset).stream()
            
            for i, user_doc in enumerate(users):
                user_data = user_doc.to_dict()
                leaderboard.append({
                    "rank": offset + i + 1,
                    "user_id": user_doc.id,
                    "name": user_data.get("name", "Anonymous"),
                    "email": user_data.get("email", ""),
                    "score": user_data.get("total_points", 0),
                    "solved": len(user_data.get("solved_problems", [])),
                    "join_date": user_data.get("created_at", "")
                })
        else:
            # Demo data
            for i in range(min(limit, 20)):
                leaderboard.append({
                    "rank": i + 1,
                    "user_id": f"user_{i}",
                    "name": f"User {i+1}",
                    "score": 100 - i * 5,
                    "solved": 10 - i,
                    "join_date": (datetime.now() - timedelta(days=i*10)).isoformat()
                })
        
        return {
            "success": True,
            "leaderboard": leaderboard,
            "timeframe": timeframe,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": len(leaderboard)
            },
            "your_rank": None  # Would include if user authenticated
        }
        
    except Exception as e:
        logger.error(f"Failed to get leaderboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get leaderboard: {str(e)}"
        )

# ==================== ADMIN ENDPOINTS ====================
@app.get("/api/admin/stats", response_model=Dict)
async def get_admin_stats(token: Optional[str] = None):
    """Get admin statistics (protected)"""
    try:
        # Simple token check (use proper auth in production)
        if token != os.environ.get("ADMIN_TOKEN", "admin123"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin access required"
            )
        
        # Get stats
        total_waveforms = len(list(WAVEFORM_DIR.glob("*.vcd")))
        
        # Count problems by difficulty
        difficulty_counts = {}
        category_counts = {}
        for problem in PROBLEMS:
            diff = problem.get("difficulty", "medium")
            category = problem.get("category", "combinational")
            difficulty_counts[diff] = difficulty_counts.get(diff, 0) + 1
            category_counts[category] = category_counts.get(category, 0) + 1
        
        return {
            "success": True,
            "platform_stats": {
                "total_problems": len(PROBLEMS),
                "total_waveforms": total_waveforms,
                "active_sessions": len(USER_SESSIONS),
                "difficulty_distribution": difficulty_counts,
                "category_distribution": category_counts,
                "problems_with_solutions": sum(1 for p in PROBLEMS if p.get("solution")),
                "problems_with_hints": sum(1 for p in PROBLEMS if p.get("hint"))
            },
            "system_stats": {
                "timestamp": datetime.now().isoformat(),
                "python_version": os.sys.version,
                "platform": os.sys.platform
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get admin stats: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get admin stats: {str(e)}"
        )

# ==================== UTILITY FUNCTIONS ====================
def run_simulation(user_code: str, testbench: str, generate_waveform: bool, problem_title: str) -> dict:
    """Run Verilog simulation with improved error handling"""
    waveform_id = None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Clean user code - remove potential malicious content
        user_code = clean_user_code(user_code)
        
        # Add waveform dump if requested
        if generate_waveform:
            waveform_id = str(uuid.uuid4())
            vcd_path = str(tmp_path / "waveform.vcd")
            
            if "$dumpfile" not in testbench:
                # Insert dump commands after initial block
                lines = testbench.split('\n')
                for i, line in enumerate(lines):
                    if line.strip().startswith("initial begin"):
                        lines.insert(i + 1, f"    $dumpfile(\"{vcd_path}\");")
                        lines.insert(i + 2, "    $dumpvars(0);")
                        break
                testbench = '\n'.join(lines)
        
        # Combine source with proper formatting
        source = f"`timescale 1ns/1ps\n// User code for: {problem_title}\n{user_code}\n\n// Testbench\n{testbench}"
        source_file = tmp_path / "design.v"
        source_file.write_text(source)
        
        # Compile with additional checks
        output_exec = tmp_path / "sim"
        compile_cmd = ["iverilog", "-g2012", "-o", str(output_exec), str(source_file)]
        
        try:
            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Compilation Timeout",
                "details": "Compilation took too long. Check for infinite loops or complex constructs."
            }
        
        if compile_result.returncode != 0:
            error_msg = compile_result.stderr[:1000]
            # Clean error message
            error_msg = re.sub(r'\/tmp\/[^:]+:', 'line ', error_msg)
            return {
                "success": False,
                "error": "Compilation Failed",
                "details": error_msg
            }
        
        # Simulate
        try:
            sim_result = subprocess.run(
                ["vvp", str(output_exec)],
                capture_output=True,
                text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Simulation Timeout",
                "details": "Simulation took too long. Possible infinite loop in testbench."
            }
        
        output = sim_result.stdout + sim_result.stderr
        
        # Check for success patterns
        success_patterns = [
            r"PASS", r"pass", r"TEST PASSED", r"All tests passed",
            r"Simulation completed successfully"
        ]
        
        is_success = any(re.search(pattern, output, re.IGNORECASE) for pattern in success_patterns)
        
        if is_success:
            result = {"success": True, "output": output}
            
            # Save waveform
            if generate_waveform and waveform_id:
                vcd_file = tmp_path / "waveform.vcd"
                if vcd_file.exists() and vcd_file.stat().st_size > 100:  # At least 100 bytes
                    dest_vcd = WAVEFORM_DIR / f"{waveform_id}.vcd"
                    shutil.copy2(vcd_file, dest_vcd)
                    
                    # Also create a JSON summary for quick access
                    summary = generate_waveform_summary(vcd_file, waveform_id)
                    if summary:
                        summary_file = WAVEFORM_DIR / f"{waveform_id}.json"
                        with open(summary_file, 'w') as f:
                            json.dump(summary, f)
                    
                    result["waveform_id"] = waveform_id
                    logger.info(f"Waveform saved: {waveform_id} ({vcd_file.stat().st_size} bytes)")
            
            return result
        else:
            # Try to extract meaningful error
            error_lines = [line for line in output.split('\n') if 'error' in line.lower() or 'fail' in line.lower()]
            error_msg = '\n'.join(error_lines[:5]) if error_lines else "Test failed - check your logic"
            
            return {
                "success": False,
                "error": "Test Failed",
                "output": output[:1500],
                "details": error_msg
            }

def clean_user_code(code: str) -> str:
    """Clean user code to prevent security issues"""
    # Remove system calls
    forbidden_patterns = [
        r'\$system\s*\(', r'\$fopen\s*\(', r'\$fwrite\s*\(', 
        r'exec\s*\(', r'system\s*\(', r'fork\s*'
    ]
    
    for pattern in forbidden_patterns:
        code = re.sub(pattern, '// REMOVED: ' + pattern, code, flags=re.IGNORECASE)
    
    return code

def generate_waveform_summary(vcd_path: Path, waveform_id: str) -> Dict:
    """Generate summary of waveform for quick loading"""
    try:
        parser = VCDParser(vcd_path)
        if parser.parse():
            return {
                "waveform_id": waveform_id,
                "signals": [
                    {
                        "name": sig["name"],
                        "short_name": sig.get("short_name", sig["name"].split('.')[-1]),
                        "width": sig.get("width", "1"),
                        "type": sig.get("type", "wire")
                    }
                    for sig in parser.signals[:50]  # Limit to 50 signals
                ],
                "timescale": parser.timescale,
                "max_time": parser.max_time,
                "signal_count": len(parser.signals),
                "generated_at": datetime.now().isoformat()
            }
    except:
        pass
    return None

def store_attempt(user_id: str, problem_id: str, code: str, passed: bool, execution_time: float):
    """Store user attempt in background"""
    try:
        if not FIREBASE_AVAILABLE:
            return
        
        attempt_data = {
            "problem_id": problem_id,
            "code": code[:5000],  # Limit code length
            "passed": passed,
            "execution_time": execution_time,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        user_ref = db.collection("users").document(user_id)
        
        # Update attempts count
        user_ref.update({
            f"attempts.{problem_id}.count": subprocess.Increment(1),
            f"attempts.{problem_id}.last_attempt": datetime.utcnow().isoformat(),
            f"attempts.{problem_id}.last_code": code[:1000],  # Store recent code
            f"attempts.{problem_id}.passed": passed
        })
        
        # Add to history
        history_ref = user_ref.collection("attempt_history").document()
        history_ref.set(attempt_data)
        
    except Exception as e:
        logger.error(f"Failed to store attempt: {e}")

def get_next_recommended_problem(user_id: str, current_problem_id: str) -> Optional[Dict]:
    """Get next recommended problem based on user progress"""
    try:
        if not FIREBASE_AVAILABLE:
            return None
        
        user_doc = db.collection("users").document(user_id).get()
        if not user_doc.exists:
            return None
        
        user_data = user_doc.to_dict()
        solved_problems = user_data.get("solved_problems", [])
        
        # Find current problem index
        current_idx = next((i for i, p in enumerate(PROBLEMS) if p["id"] == current_problem_id), -1)
        
        if current_idx == -1:
            return None
        
        # Look for next unsolved problem
        for i in range(current_idx + 1, len(PROBLEMS)):
            if PROBLEMS[i]["id"] not in solved_problems:
                next_prob = PROBLEMS[i]
                return {
                    "id": next_prob["id"],
                    "title": next_prob["title"],
                    "difficulty": next_prob.get("difficulty", "medium"),
                    "category": next_prob.get("category", "combinational"),
                    "points": next_prob.get("points", 10)
                }
        
        # If all next problems are solved, look for unsolved from beginning
        for i, problem in enumerate(PROBLEMS):
            if problem["id"] not in solved_problems:
                return {
                    "id": problem["id"],
                    "title": problem["title"],
                    "difficulty": problem.get("difficulty", "medium"),
                    "category": problem.get("category", "combinational"),
                    "points": problem.get("points", 10)
                }
        
        return None
        
    except Exception:
        return None

def check_achievements(user_data: Dict, solved_problems: List[str]) -> List[Dict]:
    """Check and award achievements"""
    achievements = []
    
    # First problem solved
    if len(solved_problems) == 1:
        achievements.append({
            "id": "first_blood",
            "name": "First Blood",
            "description": "Solved your first problem!",
            "icon": "🥇",
            "points": 10
        })
    
    # Solved 5 problems
    if len(solved_problems) == 5:
        achievements.append({
            "id": "getting_started",
            "name": "Getting Started",
            "description": "Solved 5 problems!",
            "icon": "🚀",
            "points": 25
        })
    
    # Solved problems from all categories
    categories = set()
    for problem_id in solved_problems:
        problem = next((p for p in PROBLEMS if p["id"] == problem_id), None)
        if problem:
            categories.add(problem.get("category", "unknown"))
    
    if len(categories) >= 4:
        achievements.append({
            "id": "versatile_designer",
            "name": "Versatile Designer",
            "description": "Solved problems from 4 different categories",
            "icon": "🎯",
            "points": 50
        })
    
    return achievements

def calculate_streak(streak_data: Dict) -> int:
    """Calculate login/solving streak"""
    # Simplified streak calculation
    return streak_data.get("current", 0) if streak_data else 0

class VCDParser:
    """Parse VCD files and extract waveform data"""
    def __init__(self, vcd_path):
        self.vcd_path = vcd_path
        self.signals = []
        self.waveform_data = {}
        self.timescale = "1ns"
        self.max_time = 0
    
    def parse(self):
        """Parse VCD file"""
        try:
            with open(self.vcd_path, 'r') as f:
                content = f.read()
            # ... (same parsing logic as before)
            return True
        except:
            return False
    
    def get_waveform_summary(self, signal_names=None):
        """Get waveform summary for specified signals"""
        if signal_names:
            return {name: self.waveform_data.get(name, []) for name in signal_names}
        return self.waveform_data

def create_waveform_preview(waveform_id: str, exists: bool) -> str:
    """Create simple waveform preview"""
    if not exists:
        return "<h3>Waveform not found</h3>"
    
    return f"""
    <html>
    <head>
        <title>Waveform Preview: {waveform_id}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            .preview {{ border: 1px solid #ccc; padding: 20px; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <div class="preview">
            <h3>Waveform Preview</h3>
            <p>ID: {waveform_id}</p>
            <p>Open in full viewer for detailed analysis.</p>
            <a href="/api/waveform/{waveform_id}">Open Full Viewer</a> | 
            <a href="/api/waveform/{waveform_id}?download=true">Download VCD</a>
        </div>
    </body>
    </html>
    """

def create_professional_viewer(waveform_id: str, exists: bool) -> str:
    """Create professional waveform viewer HTML"""
    # This would be the full HTML viewer from the second version
    # For brevity, returning simplified version
    return f"""
    <html>
    <head>
        <title>Waveform Viewer: {waveform_id}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f5f5f5; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
            .viewer {{ background: white; border-radius: 8px; padding: 20px; margin-top: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div class="container">
                <h1>Waveform Viewer</h1>
                <p>ID: {waveform_id}</p>
            </div>
        </div>
        <div class="container">
            <div class="viewer">
                <h3>Professional Waveform Display</h3>
                <p>Waveform visualization would appear here with interactive features.</p>
                <div style="background: #1a1a1a; color: white; padding: 20px; border-radius: 4px;">
                    <pre>// Waveform data loaded for {waveform_id}</pre>
                </div>
                <div style="margin-top: 20px;">
                    <a href="/api/waveform/{waveform_id}?download=true" style="background: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px;">Download VCD</a>
                    <button onclick="history.back()" style="padding: 10px 20px; margin-left: 10px;">Back to Editor</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

# ==================== HEALTH & MAINTENANCE ====================
@app.get("/api/health", response_model=Dict)
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "components": {
            "problems": {"status": "ok", "count": len(PROBLEMS)},
            "waveforms": {"status": "ok", "count": len(list(WAVEFORM_DIR.glob("*.vcd")))},
            "sessions": {"status": "ok", "count": len(USER_SESSIONS)},
            "firebase": {"status": "connected" if FIREBASE_AVAILABLE else "not_configured"}
        }
    }

@app.post("/api/maintenance/cleanup")
async def cleanup_old_waveforms(days_old: int = 7, token: Optional[str] = None):
    """Clean up old waveform files"""
    try:
        if token != os.environ.get("CLEANUP_TOKEN", "cleanup123"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        cutoff = datetime.now() - timedelta(days=days_old)
        deleted = 0
        
        for vcd_file in WAVEFORM_DIR.glob("*.vcd"):
            file_time = datetime.fromtimestamp(vcd_file.stat().st_mtime)
            if file_time < cutoff:
                vcd_file.unlink()
                json_file = WAVEFORM_DIR / f"{vcd_file.stem}.json"
                if json_file.exists():
                    json_file.unlink()
                deleted += 1
        
        return {
            "success": True,
            "message": f"Deleted {deleted} old waveform files",
            "remaining": len(list(WAVEFORM_DIR.glob("*.vcd")))
        }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")

# ==================== STARTUP ====================
@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Starting VLSI Practice Platform")
    logger.info(f"Loaded {len(PROBLEMS)} problems")
    logger.info(f"Waveform directory: {WAVEFORM_DIR}")
    logger.info(f"Firebase available: {FIREBASE_AVAILABLE}")
    
    # Clean old sessions
    expired_tokens = []
    for token, session in list(USER_SESSIONS.items()):
        if datetime.now() > session["expiry"]:
            expired_tokens.append(token)
    
    for token in expired_tokens:
        del USER_SESSIONS[token]
    
    if expired_tokens:
        logger.info(f"Cleaned up {len(expired_tokens)} expired sessions")

# ==================== MAIN ====================
if __name__ == "__main__":
    import uvicorn
    
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    reload = os.environ.get("RELOAD", "false").lower() == "true"
    
    logger.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        workers=int(os.environ.get("WORKERS", 1)),
        log_level="info"
    )
