# firebase_config.py
import firebase_admin
from firebase_admin import credentials, auth, firestore
import os
import json

# Try to initialize Firebase
def init_firebase():
    try:
        # Method 1: Service account JSON file
        if os.path.exists("firebase-config.json"):
            cred = credentials.Certificate("firebase-config.json")
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized from file")
            return firestore.client()
        
        # Method 2: Environment variable (for Render/Heroku)
        elif os.environ.get("FIREBASE_CONFIG"):
            config_json = json.loads(os.environ["FIREBASE_CONFIG"])
            cred = credentials.Certificate(config_json)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized from env")
            return firestore.client()
        
        else:
            print("⚠️ Firebase not configured - running in local mode")
            return None
            
    except Exception as e:
        print(f"⚠️ Firebase init failed: {e} - running in local mode")
        return None

# Initialize Firebase
db = init_firebase()
