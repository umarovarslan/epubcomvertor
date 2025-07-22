# EPUB to PDF Converter

A professional web application that converts EPUB files to beautifully formatted PDF documents with customizable settings.

## Features

### Core Functionality
- **EPUB to PDF Conversion**: Convert any EPUB file from a URL to a high-quality PDF
- **Custom Images**: Add cover images, title page backgrounds, and full-page images
- **PDF Settings**: Adjust font size (8-20pt), line spacing (1.0-2.0x), and page margins (0.5-1.5")
- **Real-time Progress**: Live conversion status with detailed progress tracking
- **Professional Layout**: Includes table of contents, headers, footers, and page numbering

### User Interface
- **Modern Design**: Clean, responsive interface that works on desktop and mobile
- **Easy to Use**: Simple form-based interface with intuitive controls
- **Visual Feedback**: Progress bars, status messages, and download notifications
- **Professional Styling**: Gradient backgrounds, smooth animations, and modern typography

## How to Use

### 1. Basic Conversion
1. Enter the EPUB file URL in the "EPUB URL" field
2. Click "Convert to PDF" to start the conversion
3. Wait for the conversion to complete
4. Click "Download PDF" to get your converted file

### 2. Custom Images (Optional)
- **Cover Image URL**: Image to use as the book cover
- **Title Page Background URL**: Background image for the title page
- **Full Page Image URL**: Additional full-page image to include

### 3. PDF Settings
- **Font Size**: Adjust text size from 8pt to 20pt (default: 13pt)
- **Line Spacing**: Control line height from 1.0x to 2.0x (default: 1.5x)
- **Page Margin**: Set page margins from 0.5" to 1.5" (default: 1.0")

## Supported Formats

### Input
- **EPUB files**: Standard EPUB format from any accessible URL
- **Images**: JPG, PNG, GIF formats for customization images

### Output
- **PDF**: High-quality PDF with embedded fonts and images
- **Features**: Table of contents, headers, footers, page numbers
- **Layout**: Professional book-style formatting

## Technical Details

### Built With
- **Backend**: Python Flask with specialized EPUB processing libraries
- **Frontend**: Modern HTML5, CSS3, and JavaScript
- **PDF Generation**: ReportLab for professional PDF creation
- **Image Processing**: PIL/Pillow for image optimization

### Libraries Used
- **EbookLib**: EPUB file parsing and content extraction
- **BeautifulSoup**: HTML content processing
- **ReportLab**: PDF document generation
- **Pillow**: Image processing and manipulation
- **Flask**: Web application framework

### Browser Compatibility
- Chrome/Chromium 80+
- Firefox 75+
- Safari 13+
- Edge 80+
- Mobile browsers (iOS Safari, Chrome Mobile)

## Example Usage

### Sample EPUB URLs
The application comes pre-configured with sample URLs for testing:
- EPUB: `https://flibusta.is/b/416925/epub`
- Cover: `https://flibusta.is/i/25/416925/cover.jpg`
- Title Background: `https://bekbookstore.com/titlepage.jpg`
- Full Page Image: `https://bekbookstore.com/fullpageimage.jpg`

### Conversion Process
1. **Fetching**: Downloads the EPUB file from the provided URL
2. **Processing**: Extracts content, images, and metadata
3. **Image Preparation**: Downloads and processes custom images
4. **PDF Generation**: Creates formatted PDF with all content
5. **Cleanup**: Removes temporary files after conversion

## Tips for Best Results

### EPUB Files
- Use publicly accessible URLs
- Ensure the EPUB file is not corrupted
- Larger files may take longer to process

### Custom Images
- Use high-resolution images for better quality
- Recommended formats: JPG, PNG
- Images will be automatically resized to fit the page

### PDF Settings
- **Font Size**: 12-14pt works well for most books
- **Line Spacing**: 1.4-1.6x provides good readability
- **Margins**: 1.0" is standard for most documents

## Limitations

- Maximum file size: 100MB for EPUB files
- Processing time varies based on file size and complexity
- Requires internet connection for downloading EPUB files and images
- Some complex EPUB layouts may not convert perfectly

## Privacy and Security

- Files are processed temporarily and automatically deleted
- No user data is stored permanently
- All conversions are processed securely
- Generated PDFs are available for download for 1 hour only

## Support

If you encounter any issues:
1. Check that your EPUB URL is accessible
2. Verify that custom image URLs are valid
3. Try with different PDF settings if conversion fails
4. Ensure stable internet connection during conversion

## Version Information

- **Version**: 1.0.0
- **Last Updated**: 2025
- **Compatibility**: Python 3.8+, Modern Web Browsers

---

**Note**: This application is designed for converting publicly available EPUB files. Please respect copyright laws and only convert files you have the right to use.

