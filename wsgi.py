import os
import sys

# Get the directory where wsgi.py is located
current_dir = os.path.dirname(os.path.abspath(__file__))

# Add the parent directory to Python path (where src folder should be)
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# Also add current directory in case src is at the same level
sys.path.insert(0, current_dir)

try:
    from src.main import app
except ImportError:
    # If src.main doesn't work, try importing main directly
    from main import app

# Configure for production
app.config["DEBUG"] = False

if __name__ == "__main__":
    # This is only used for development
    app.run(host="0.0.0.0", port=5000, debug=False)
