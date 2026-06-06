"""
betPawa Virtual Sports — Entry point for both local and Render deployment.

Local:  python run.py
Render: gunicorn run:app (uses PORT env var automatically)
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from app import app, seed
from match_engine import start_scheduler

# Initialise DB and seed on first boot
with app.app_context():
    seed()

# Start background match scheduler
start_scheduler(app)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"\n{'='*56}")
    print(f"  betPawa Virtual Sports → http://localhost:{port}")
    print(f"  Admin : admin / admin123  →  /admin")
    print(f"  Demo  : demo  / demo123")
    print(f"{'='*56}\n")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
