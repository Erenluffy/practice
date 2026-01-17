// In your backend server (e.g., index.js or routes/progress.js)

const express = require('express');
const router = express.Router();
const admin = require('firebase-admin');

// Initialize Firebase Admin SDK
const serviceAccount = require('./path/to/serviceAccountKey.json');

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: 'https://your-project-id.firebaseio.com'
});

// Update solved status in Firebase
router.post('/solved', authenticateToken, async (req, res) => {
  try {
    const { user_id, problem_id, solved_at, points_earned, solution_hash } = req.body;
    
    // Update in Firebase Firestore
    const db = admin.firestore();
    
    // Get user document
    const userRef = db.collection('users').doc(user_id);
    const userDoc = await userRef.get();
    
    if (!userDoc.exists) {
      // Create user document if it doesn't exist
      await userRef.set({
        email: req.user.email,
        name: req.user.name,
        created_at: new Date().toISOString(),
        total_points: points_earned || 0,
        solved_problems: [problem_id]
      });
    } else {
      // Update existing user document
      const userData = userDoc.data();
      const solvedProblems = userData.solved_problems || [];
      
      if (!solvedProblems.includes(problem_id)) {
        solvedProblems.push(problem_id);
        
        await userRef.update({
          solved_problems: solvedProblems,
          total_points: (userData.total_points || 0) + (points_earned || 10),
          updated_at: new Date().toISOString()
        });
        
        // Add to solved_problems collection for analytics
        await db.collection('solved_problems').add({
          user_id: user_id,
          problem_id: problem_id,
          solved_at: solved_at || new Date().toISOString(),
          points_earned: points_earned || 10,
          solution_hash: solution_hash || '',
          created_at: new Date().toISOString()
        });
      }
    }
    
    res.json({
      success: true,
      message: 'Problem solved status updated in Firebase',
      problem_id: problem_id
    });
    
  } catch (error) {
    console.error('Firebase update error:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to update Firebase'
    });
  }
});

// Get solved problems from Firebase
router.get('/solved', authenticateToken, async (req, res) => {
  try {
    const db = admin.firestore();
    const userRef = db.collection('users').doc(req.user.id);
    const userDoc = await userRef.get();
    
    if (userDoc.exists) {
      const userData = userDoc.data();
      res.json({
        success: true,
        solved_problems: userData.solved_problems || []
      });
    } else {
      res.json({
        success: true,
        solved_problems: []
      });
    }
    
  } catch (error) {
    console.error('Firebase fetch error:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to fetch from Firebase'
    });
  }
});

// Sync local solved problems to Firebase
router.post('/sync', authenticateToken, async (req, res) => {
  try {
    const { user_id, solved_problems, sync_timestamp } = req.body;
    
    const db = admin.firestore();
    const userRef = db.collection('users').doc(user_id);
    
    // Get current solved problems
    const userDoc = await userRef.get();
    const currentData = userDoc.exists ? userDoc.data() : { solved_problems: [] };
    const currentSolved = new Set(currentData.solved_problems || []);
    
    // Merge with new solved problems
    solved_problems.forEach(problemId => currentSolved.add(problemId));
    
    const mergedSolved = Array.from(currentSolved);
    
    // Update user document
    await userRef.set({
      ...currentData,
      solved_problems: mergedSolved,
      total_points: mergedSolved.length * 10, // 10 points per problem
      updated_at: new Date().toISOString(),
      last_sync: sync_timestamp
    }, { merge: true });
    
    res.json({
      success: true,
      message: 'Sync successful',
      solved_count: mergedSolved.length
    });
    
  } catch (error) {
    console.error('Firebase sync error:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to sync with Firebase'
    });
  }
});

// Authentication middleware
function authenticateToken(req, res, next) {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];
  
  if (!token) {
    return res.status(401).json({ error: 'No token provided' });
  }
  
  // Verify JWT token (implement your token verification logic)
  // This depends on how you're handling authentication
  
  next();
}

module.exports = router;
