# firebase_config.py - SIMPLE WORKING VERSION
import os
import json

# Set a flag based on environment
FIREBASE_AVAILABLE = False
db = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, initialize_app
    
    # Check if we have the environment variable
    firebase_key = os.environ.get("FIREBASE_KEY_JSON")
    
    if firebase_key:
        try:
            # Clean the key (Render sometimes adds quotes)
            if firebase_key.startswith('"') and firebase_key.endswith('"'):
                firebase_key = firebase_key[1:-1]
            
            # Fix the newline issue - replace escaped newlines with actual newlines
            firebase_key = firebase_key.replace('\\n', '\n')
            
            # Parse JSON
            key_data = json.loads(firebase_key)
            
            # Initialize
            cred = credentials.Certificate(key_data)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            FIREBASE_AVAILABLE = True
            
            print("🎉 Firebase initialized successfully!")
        except json.JSONDecodeError as e:
            print(f"❌ JSON decode error: {e}")
            print("First 200 chars of key:", firebase_key[:200])
        except Exception as e:
            print(f"❌ Firebase init failed: {e}")
    else:
        print("ℹ️ FIREBASE_KEY_JSON not set - running without Firebase")
        
except ImportError:
    print("ℹ️ firebase-admin not installed - running without Firebase")
except Exception as e:
    print(f"⚠️ Firebase setup error: {e}")
