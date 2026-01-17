# firebase_config.py - FIXED VERSION
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

def init_firebase():
    try:
        # Check if already initialized
        if firebase_admin._apps:
            print("✅ Firebase already initialized")
            return firestore.client()
        
        # Method 1: Check for environment variable (Render/Production)
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            # This is the standard environment variable name
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {
                'projectId': 'vlsiverse'  # Your project ID
            })
            print("✅ Firebase initialized with GOOGLE_APPLICATION_CREDENTIALS")
            return firestore.client()
        
        # Method 2: Direct JSON from environment variable
        elif os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY"):
            # Parse JSON from environment variable
            service_account_info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_KEY"])
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized from FIREBASE_SERVICE_ACCOUNT_KEY")
            return firestore.client()
        
        # Method 3: JSON file for local development
        elif os.path.exists("firebase-config.json"):
            cred = credentials.Certificate("firebase-config.json")
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized from firebase-config.json file")
            return firestore.client()
        
        # Method 4: Individual environment variables
        elif all(key in os.environ for key in ["FIREBASE_PROJECT_ID", "FIREBASE_PRIVATE_KEY"]):
            service_account_info = {
                "type": "service_account",
                "project_id": os.environ["FIREBASE_PROJECT_ID"],
                "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": os.environ["FIREBASE_PRIVATE_KEY"].replace('\\n', '\n'),
                "client_email": os.environ.get("FIREBASE_CLIENT_EMAIL", ""),
                "client_id": os.environ.get("FIREBASE_CLIENT_ID", ""),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": os.environ.get("FIREBASE_CLIENT_X509_CERT_URL", "")
            }
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized from separate env vars")
            return firestore.client()
        
        else:
            print("⚠️ Firebase not configured - running in local mode")
            print("ℹ️ Set GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_SERVICE_ACCOUNT_KEY")
            return None
            
    except Exception as e:
        print(f"❌ Firebase initialization failed: {str(e)}")
        print("⚠️ Continuing in local/demo mode")
        return None

# Initialize Firebase
db = init_firebase()

# Test function
def test_firebase_connection():
    """Test if Firebase is working"""
    if db:
        try:
            # Try to list users (requires authentication permissions)
            # Just testing if we can access Firestore
            test_ref = db.collection("_test").document("connection")
            test_ref.set({"test": True, "timestamp": firestore.SERVER_TIMESTAMP})
            test_ref.delete()
            print("✅ Firebase connection test successful")
            return True
        except Exception as e:
            print(f"❌ Firebase test failed: {e}")
            return False
    return False
