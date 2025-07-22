#!/usr/bin/env python3
"""
WSGI Entry Point for EPUB to PDF Converter
This file is used by production WSGI servers like Gunicorn
"""

import os
import sys

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.main import app

# Configure for production
app.config['DEBUG'] = False

if __name__ == "__main__":
    # This is only used for development
    app.run(host='0.0.0.0', port=5000, debug=False)

