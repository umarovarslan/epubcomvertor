# EPUB to PDF Converter - Deployment Guide

## Overview

This web application converts EPUB files to PDF format with customizable settings. It's built with Flask (Python backend) and vanilla HTML/CSS/JavaScript (frontend).

## Features

- Convert EPUB files from URLs to PDF format
- Customizable cover images, title page backgrounds, and full-page images
- Adjustable PDF settings (font size, line spacing, page margins)
- Real-time conversion progress tracking
- Professional, responsive web interface
- Automatic cleanup of temporary files

## System Requirements

### Server Requirements
- Python 3.8 or higher
- Linux/Unix environment (Ubuntu/CentOS recommended)
- At least 1GB RAM
- 2GB free disk space
- Internet connectivity for downloading EPUB files and images

### Required System Packages
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv fonts-dejavu-core

# CentOS/RHEL
sudo yum install -y python3 python3-pip dejavu-sans-fonts
```

## Deployment Options

### Option 1: Hostinger VPS/Cloud Hosting

#### Step 1: Upload Files
1. Upload the entire `epub-pdf-converter` folder to your server
2. Connect via SSH to your Hostinger server

#### Step 2: Setup Environment
```bash
cd /path/to/epub-pdf-converter
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Step 3: Configure for Production
Create a production configuration file `production_config.py`:
```python
import os

class ProductionConfig:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-here'
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB
    DEBUG = False
```

#### Step 4: Create WSGI Application
Create `wsgi.py` in the root directory:
```python
from src.main import app

if __name__ == "__main__":
    app.run()
```

#### Step 5: Setup with Gunicorn
```bash
pip install gunicorn
gunicorn --bind 0.0.0.0:8000 wsgi:app
```

#### Step 6: Configure Reverse Proxy (Apache/Nginx)
For Apache, add to your virtual host:
```apache
ProxyPass / http://localhost:8000/
ProxyPassReverse / http://localhost:8000/
```

For Nginx:
```nginx
location / {
    proxy_pass http://localhost:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Option 2: Shared Hosting (Limited Support)

Most shared hosting providers don't support Flask applications. Consider upgrading to VPS or cloud hosting.

### Option 3: Cloud Platforms

#### Heroku
1. Create `Procfile`:
```
web: gunicorn wsgi:app
```

2. Create `runtime.txt`:
```
python-3.11.0
```

3. Deploy:
```bash
git init
git add .
git commit -m "Initial commit"
heroku create your-app-name
git push heroku main
```

#### DigitalOcean App Platform
1. Connect your Git repository
2. Set build command: `pip install -r requirements.txt`
3. Set run command: `gunicorn wsgi:app`

## Configuration

### Environment Variables
Set these environment variables for production:

```bash
export SECRET_KEY="your-very-secret-key-here"
export FLASK_ENV="production"
```

### Security Considerations
1. Change the default SECRET_KEY in production
2. Enable HTTPS/SSL
3. Set up proper firewall rules
4. Regular security updates
5. Monitor server resources

## File Structure
```
epub-pdf-converter/
├── src/
│   ├── main.py              # Main Flask application
│   ├── routes/
│   │   └── converter.py     # Conversion API endpoints
│   └── static/
│       └── index.html       # Frontend interface
├── venv/                    # Virtual environment
├── requirements.txt         # Python dependencies
├── wsgi.py                 # WSGI entry point
├── DEPLOYMENT_GUIDE.md     # This file
└── README.md               # User documentation
```

## API Endpoints

- `POST /api/convert` - Start EPUB to PDF conversion
- `GET /api/status/<conversion_id>` - Check conversion status
- `GET /api/download/<conversion_id>` - Download generated PDF
- `POST /api/cleanup` - Manual cleanup of old conversions

## Troubleshooting

### Common Issues

#### 1. Font Issues
**Problem**: PDF generation fails with font errors
**Solution**: 
```bash
sudo apt-get install fonts-dejavu-core
# Or manually install DejaVu Sans font
```

#### 2. Memory Issues
**Problem**: Conversion fails for large EPUB files
**Solution**: 
- Increase server RAM
- Add swap space
- Set appropriate timeout values

#### 3. Permission Issues
**Problem**: Cannot write temporary files
**Solution**:
```bash
chmod 755 /tmp
# Or set custom temp directory with proper permissions
```

#### 4. Network Issues
**Problem**: Cannot download EPUB files or images
**Solution**:
- Check firewall settings
- Verify internet connectivity
- Check if URLs are accessible

### Log Files
Check application logs for detailed error information:
```bash
# If using systemd service
journalctl -u your-service-name -f

# If running manually
python src/main.py 2>&1 | tee app.log
```

## Monitoring and Maintenance

### Regular Tasks
1. Monitor disk space (temporary files cleanup)
2. Check server resources (CPU, RAM usage)
3. Update dependencies regularly
4. Backup configuration files

### Performance Optimization
1. Use a reverse proxy (Nginx/Apache)
2. Enable gzip compression
3. Set up caching for static files
4. Monitor conversion times and optimize if needed

## Support

For technical support or questions:
1. Check the troubleshooting section above
2. Review server logs for error details
3. Ensure all system requirements are met
4. Verify network connectivity and permissions

## Security Notes

- Never expose debug mode in production
- Use strong, unique SECRET_KEY
- Implement rate limiting for API endpoints
- Regular security updates
- Monitor for suspicious activity
- Use HTTPS in production

## License

This application is provided as-is for deployment purposes. Ensure compliance with all relevant licenses for included libraries and dependencies.

