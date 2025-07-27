import os
import sys
import requests
import re
import html
import tempfile
import io
import threading
import uuid
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email_validator import validate_email, EmailNotValidError
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_file
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, NextPageTemplate, PageBreak, Image as ReportLabImage)
from reportlab.platypus.flowables import KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import PyPDF2

converter_bp = Blueprint('converter', __name__)

# Global storage for conversion status
conversion_status = {}

class PageDrawer:
    """Helper class to manage data for ReportLab's onPage functions."""
    def __init__(self, cover_path, title_bg_path, blurred_cover_path, full_page_image_path,
                 book_title, author_name, inner_margin, outer_margin, top_bottom_margin):
        self.cover_path = cover_path
        self.title_page_bg_path = title_bg_path
        self.blurred_cover_path = blurred_cover_path
        self.full_page_image_path = full_page_image_path
        self.book_title = book_title
        self.author_name = author_name
        self.inner_margin = inner_margin
        self.outer_margin = outer_margin
        self.top_bottom_margin = top_bottom_margin

    def cover_and_content_pages(self, canvas, doc):
        canvas.saveState()
        page_width, page_height = letter
        page_num = canvas.getPageNumber()

        if page_num == 1:  # Cover Page
            if os.path.exists(self.cover_path):
                canvas.drawImage(self.cover_path, 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)
        elif page_num > 2:  # Content Pages (skip title page)
            canvas.setFont('DejaVu-Sans', 9)
            header_y = page_height - self.top_bottom_margin + inch * 0.15

            # Headers are on the inner top margin
            if page_num % 2 != 0:  # Odd pages (right-hand pages, left margin is inner)
                canvas.drawString(self.inner_margin, header_y, self.author_name)
            else:  # Even pages (left-hand pages, right margin is inner)
                canvas.drawRightString(page_width - self.inner_margin, header_y, self.book_title)

            if page_num > 3: # Page numbering starts after TOC
                canvas.drawCentredString(page_width / 2.0, self.top_bottom_margin - inch * 0.25, str(page_num - 3))
        canvas.restoreState()

    def title_page_background(self, canvas, doc):
        canvas.saveState()
        if os.path.exists(self.title_page_bg_path):
            canvas.drawImage(self.title_page_bg_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()

    def full_image_page_background(self, canvas, doc):
        canvas.saveState()
        if self.full_page_image_path and os.path.exists(self.full_page_image_path):
            canvas.drawImage(self.full_page_image_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()

    def final_page_background(self, canvas, doc):
        canvas.saveState()
        if os.path.exists(self.blurred_cover_path):
            canvas.drawImage(self.blurred_cover_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()


class EpubToPdfConverter:
    def __init__(self):
        # Register DejaVu Sans font (we'll need to include this font file)
        font_path = self.get_font_path()
        if font_path and os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont('DejaVu-Sans', font_path))
        else:
            # Fallback to system fonts
            pdfmetrics.registerFont(TTFont('DejaVu-Sans', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))

    def get_font_path(self):
        """Try to find DejaVu Sans font in common locations"""
        possible_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/System/Library/Fonts/DejaVuSans.ttf',
            os.path.join(os.path.dirname(__file__), '..', 'static', 'fonts', 'DejaVuSans.ttf')
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    def count_pdf_pages(self, pdf_path):
        """Count the number of pages in a PDF file"""
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                return len(pdf_reader.pages)
        except Exception as e:
            print(f"Error counting PDF pages: {e}")
            return 0

    def get_default_email_body(self):
        """Get the default email body template"""
        return """Hello!

Your EPUB to PDF conversion is complete!

Book Title: {book_title}
Total Pages: {page_count}

Please find the converted PDF attached to this email.

Best regards,
EPUB to PDF Converter"""

    def process_email_body(self, email_body, book_title, page_count):
        """Process email body by replacing placeholders with actual values"""
        if not email_body or email_body.strip() == "":
            email_body = self.get_default_email_body()
        
        # Replace placeholders with actual values
        processed_body = email_body.replace("{book_title}", str(book_title))
        processed_body = processed_body.replace("{page_count}", str(page_count))
        
        return processed_body

    def send_email_with_pdf(self, recipient_email, pdf_path, book_title, page_count, custom_email_body=None):
        """Send PDF via email with custom email body support"""
        try:
            # Email configuration - using Gmail SMTP as example
            # In production, these should be environment variables
            smtp_server = "smtp.gmail.com"
            smtp_port = 587
            sender_email = "mr.umaroff@gmail.com"  # Replace with actual sender email
            sender_password = "mhwb iwfn epsc glnt"  # Replace with actual app password
            
            # Create message
            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = recipient_email
            msg['Subject'] = f"Your converted PDF: {book_title}"
            
            # Process email body with custom content or use default
            email_body = self.process_email_body(custom_email_body, book_title, page_count)
            
            msg.attach(MIMEText(email_body, 'plain'))
            
            # Attach PDF
            with open(pdf_path, 'rb') as f:
                pdf_attachment = MIMEApplication(f.read(), _subtype='pdf')
                safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
                pdf_attachment.add_header('Content-Disposition', 'attachment', filename=f"{safe_title}.pdf")
                msg.attach(pdf_attachment)
            
            # Send email
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(sender_email, sender_password)
            text = msg.as_string()
            server.sendmail(sender_email, recipient_email, text)
            server.quit()
            
            return True
            
        except Exception as e:
            print(f"Error sending email: {e}")
            return False

    def convert_epub_to_pdf_and_email(self, conversion_id, params, recipient_email):
        """Convert EPUB to PDF and send via email"""
        try:
            # Validate email first
            try:
                validate_email(recipient_email)
            except EmailNotValidError:
                conversion_status[conversion_id] = {
                    'status': 'error',
                    'progress': 0,
                    'message': 'Invalid email address provided',
                    'created_at': datetime.now()
                }
                return

            # First do the regular conversion
            self.convert_epub_to_pdf(conversion_id, params)
            
            # Check if conversion was successful
            if conversion_status[conversion_id]['status'] == 'completed':
                conversion_status[conversion_id]['progress'] = 95
                conversion_status[conversion_id]['message'] = 'Sending PDF via email...'
                
                pdf_path = conversion_status[conversion_id]['pdf_path']
                book_title = conversion_status[conversion_id]['book_title']
                page_count = conversion_status[conversion_id]['page_count']
                
                # Get custom email body from params
                custom_email_body = params.get('email_body', None)
                
                # Send email with custom body
                if self.send_email_with_pdf(recipient_email, pdf_path, book_title, page_count, custom_email_body):
                    conversion_status[conversion_id]['message'] = f'PDF sent successfully to {recipient_email}! ({page_count} pages)'
                    conversion_status[conversion_id]['progress'] = 100
                    conversion_status[conversion_id]['email_sent'] = True
                    conversion_status[conversion_id]['recipient_email'] = recipient_email
                else:
                    conversion_status[conversion_id]['status'] = 'error'
                    conversion_status[conversion_id]['message'] = 'PDF generated but failed to send email'
                    
        except Exception as e:
            conversion_status[conversion_id] = {
                'status': 'error',
                'progress': 0,
                'message': f'An error occurred: {str(e)}',
                'created_at': datetime.now()
            }

    def flatten_toc(self, toc_list):
        """
        FIXED: Properly handle different types of TOC structures.
        The error was caused by trying to iterate over a single epub.Link object
        when the TOC structure was not nested.
        """
        flat_list = []
        
        # Handle case where toc_list might be a single Link object
        if isinstance(toc_list, epub.Link):
            return [toc_list]
        
        # Handle case where toc_list is not iterable (None, etc.)
        if not hasattr(toc_list, '__iter__'):
            return []
        
        for item in toc_list:
            if isinstance(item, (list, tuple)):
                # Recursively flatten nested lists/tuples
                flat_list.extend(self.flatten_toc(item))
            elif isinstance(item, epub.Link):
                # Add Link objects directly
                flat_list.append(item)
            elif hasattr(item, '__iter__') and not isinstance(item, (str, bytes)):
                # Handle other iterable types that might contain Links
                flat_list.extend(self.flatten_toc(item))
        
        return flat_list

    def get_image_path(self, image_input, temp_filename, temp_dir):
        if not image_input:
            return None
        if image_input.startswith("http"):
            try:
                response = requests.get(image_input, timeout=30)
                response.raise_for_status()
                image_path = os.path.join(temp_dir, temp_filename)
                with open(image_path, 'wb') as f:
                    f.write(response.content)
                return image_path
            except requests.RequestException as e:
                raise IOError(f"Failed to download image {image_input}: {e}")
        else:
            if os.path.exists(image_input):
                return image_input
            else:
                raise FileNotFoundError(f"Image file not found: {image_input}")

    def build_story(self, doc, book_title, author_name, book_description, toc_items, content_map,
                    image_map, font_size, line_spacing, has_full_page_image,
                    frame_width, frame_height):
        story = []
        styles = getSampleStyleSheet()
        leading = font_size * line_spacing

        body_style = ParagraphStyle('BodyText', parent=styles['Normal'], fontName='DejaVu-Sans',
                                      fontSize=font_size, leading=leading, alignment=TA_JUSTIFY)
        h1_style = ParagraphStyle('H1', parent=styles['h1'], fontName='DejaVu-Sans',
                                    fontSize=20, leading=24, spaceAfter=12, alignment=TA_CENTER)
        toc_style = ParagraphStyle('TOC', parent=styles['Normal'], fontName='DejaVu-Sans',
                                     fontSize=14, leading=18, leftIndent=inch*0.25)
        title_page_title_style = ParagraphStyle('TitlePageTitle', parent=styles['h1'], fontName='DejaVu-Sans',
                                                  fontSize=30, textColor=colors.black, alignment=TA_CENTER)
        title_page_author_style = ParagraphStyle('TitlePageAuthor', parent=styles['Normal'], fontName='DejaVu-Sans',
                                                   fontSize=18, textColor=colors.black, alignment=TA_CENTER, spaceBefore=12)
        description_style = ParagraphStyle('Description', parent=body_style, textColor=colors.white,
                                             backColor=colors.Color(0,0,0,0.6), alignment=TA_CENTER,
                                             borderPadding=20, borderRadius=15)

        # Title page
        story.append(NextPageTemplate('TitlePage'))
        story.append(PageBreak())

        title_page_content = [
            Spacer(1, 3*inch),
            Paragraph(book_title, title_page_title_style),
            Spacer(1, 0.25*inch),
            Paragraph(f"<i>{author_name}</i>", title_page_author_style)
        ]
        story.append(KeepInFrame(letter[0], letter[1], title_page_content, vAlign='TOP'))
        
        # Set up alternating page templates for the main content
        story.append(NextPageTemplate(['OddContentPage', 'EvenContentPage']))
        story.append(PageBreak())

        # Table of contents
        toc_page_content = [Paragraph("Содержание", h1_style), Spacer(1, 0.25*inch)]
        chapter_content_story = []
        toc_links = []

        for i, item in enumerate(toc_items):
            bookmark_key = f'toc_entry_{i}'
            toc_links.append((item.title, bookmark_key))
            chapter_content_story.append(PageBreak())
            title_with_anchor = f'<a name="{bookmark_key}"/>{item.title}'
            chapter_content_story.append(Paragraph(title_with_anchor, h1_style))

            chapter_html = content_map.get(item.href.split('#')[0])
            if chapter_html:
                soup = BeautifulSoup(chapter_html, 'html.parser')
                for tag in soup.find_all(['p', 'img']):
                    if tag.name == 'p' and tag.get_text(strip=True):
                        chapter_content_story.append(Paragraph(tag.get_text(strip=True), body_style))
                        chapter_content_story.append(Spacer(1, 0.1 * inch))
                    elif tag.name == 'img' and tag.get('src'):
                        img_src_base = os.path.basename(tag['src'])
                        if img_src_base in image_map:
                            try:
                                img_data = io.BytesIO(image_map[img_src_base])
                                with Image.open(img_data) as pil_img:
                                    img_width, img_height = pil_img.size

                                V_BUFFER = 1 * inch
                                max_width = frame_width
                                max_height = frame_height - V_BUFFER

                                display_width = img_width
                                display_height = img_height

                                if display_width > max_width or display_height > max_height:
                                    width_ratio = max_width / display_width
                                    height_ratio = max_height / display_height
                                    scale_ratio = min(width_ratio, height_ratio)

                                    display_width = display_width * scale_ratio
                                    display_height = display_height * scale_ratio

                                img_data.seek(0)
                                rl_image = ReportLabImage(img_data, width=display_width, height=display_height)
                                chapter_content_story.append(rl_image)
                                chapter_content_story.append(Spacer(1, 0.2 * inch))
                            except Exception:
                                pass  # Skip problematic images

        for title, key in toc_links:
            toc_page_content.append(Paragraph(f'<a href="#{key}">{title}</a>', toc_style))

        story.extend(toc_page_content)
        story.extend(chapter_content_story)

        # Full page image if provided
        if has_full_page_image:
            story.append(NextPageTemplate('FullImagePage'))
            story.append(PageBreak())
            story.append(Spacer(1, 8*inch))  # Placeholder content

        # Final page with blurred cover
        story.append(NextPageTemplate('FinalPage'))
        story.append(PageBreak())
        story.append(Spacer(1, 3*inch))
        story.append(Paragraph(book_description, description_style))

        return story

    def convert_epub_to_pdf(self, conversion_id, params):
        """Main conversion logic"""
        try:
            conversion_status[conversion_id] = {
                'status': 'processing',
                'progress': 0,
                'message': 'Starting conversion...',
                'created_at': datetime.now()
            }

            # Extract parameters
            epub_url = params.get('epub_url')
            cover_input = params.get('cover_input', '')
            title_page_bg_input = params.get('title_page_bg_input', '')
            full_page_image_input = params.get('full_page_image_input', '')
            font_size = int(params.get('font_size', 13))
            line_spacing = float(params.get('line_spacing', 1.5))
            
            # ** NEW MARGIN PARAMETERS **
            inner_margin = float(params.get('inner_margin', 0.75)) * inch
            outer_margin = float(params.get('outer_margin', 1.20)) * inch
            top_bottom_margin = float(params.get('top_bottom_margin', 0.75)) * inch

            conversion_status[conversion_id]['progress'] = 5
            conversion_status[conversion_id]['message'] = 'Fetching EPUB file...'

            # Fetch EPUB
            response = requests.get(epub_url, timeout=60)
            response.raise_for_status()

            temp_dir = tempfile.mkdtemp()
            epub_path = os.path.join(temp_dir, "book.epub")
            with open(epub_path, 'wb') as f:
                f.write(response.content)
            book = epub.read_epub(epub_path)

            conversion_status[conversion_id]['progress'] = 15
            conversion_status[conversion_id]['message'] = 'Processing EPUB content...'

            # Extract metadata
            book_title, author_name, book_description = "Unknown Title", "Unknown Author", "No description found."
            if book.get_metadata('DC', 'title'):
                book_title = book.get_metadata('DC', 'title')[0][0]
            if book.get_metadata('DC', 'creator'):
                author_name = book.get_metadata('DC', 'creator')[0][0]
            if book.get_metadata('DC', 'description'):
                raw_desc = book.get_metadata('DC', 'description')[0][0]
                book_description = html.unescape(re.sub('<[^<]+?>', '', raw_desc))

            # Map content and images
            toc_items = self.flatten_toc(book.toc)
            content_map = {item.get_name(): item.get_content() for item in book.get_items_of_type(ITEM_DOCUMENT)}
            image_map = {os.path.basename(item.get_name()): item.get_content() for item in book.get_items_of_type(ITEM_IMAGE)}

            conversion_status[conversion_id]['progress'] = 25
            conversion_status[conversion_id]['message'] = 'Preparing images...'

            # Process images
            cover_path = self.get_image_path(cover_input, "cover.jpg", temp_dir) if cover_input else None
            title_bg_path = self.get_image_path(title_page_bg_input, "title_bg.jpg", temp_dir) if title_page_bg_input else None
            full_page_image_path = self.get_image_path(full_page_image_input, "full_page_image.jpg", temp_dir) if full_page_image_input else None

            # Create blurred cover if cover exists
            blurred_cover_path = None
            if cover_path and os.path.exists(cover_path):
                blurred_cover_path = os.path.join(temp_dir, "blurred_cover.jpg")
                with Image.open(cover_path) as img:
                    img_resized = img.resize((int(letter[0]), int(letter[1])))
                    img_resized.filter(ImageFilter.GaussianBlur(25)).save(blurred_cover_path)

            conversion_status[conversion_id]['progress'] = 40
            conversion_status[conversion_id]['message'] = 'Building PDF structure...'

            # Create PDF
            pdf_path = os.path.join(temp_dir, "converted_book.pdf")
            
            # Calculate frame dimensions based on margins
            frame_width = letter[0] - inner_margin - outer_margin
            frame_height = letter[1] - 2 * top_bottom_margin

            # Create page drawer with margin information
            page_drawer = PageDrawer(
                cover_path or "", title_bg_path or "", blurred_cover_path or "", 
                full_page_image_path, book_title, author_name,
                inner_margin, outer_margin, top_bottom_margin
            )

            # Create document with page templates
            doc = BaseDocTemplate(pdf_path, pagesize=letter)

            # Define frames for different page types
            content_frame_odd = Frame(inner_margin, top_bottom_margin, frame_width, frame_height, id='content_odd')
            content_frame_even = Frame(outer_margin, top_bottom_margin, frame_width, frame_height, id='content_even')
            title_frame = Frame(inch, inch, letter[0] - 2*inch, letter[1] - 2*inch, id='title')
            full_frame = Frame(0, 0, letter[0], letter[1], id='full')

            # Define page templates
            cover_template = PageTemplate(id='CoverPage', frames=[full_frame], onPage=page_drawer.cover_and_content_pages)
            title_template = PageTemplate(id='TitlePage', frames=[title_frame], onPage=page_drawer.title_page_background)
            odd_template = PageTemplate(id='OddContentPage', frames=[content_frame_odd], onPage=page_drawer.cover_and_content_pages)
            even_template = PageTemplate(id='EvenContentPage', frames=[content_frame_even], onPage=page_drawer.cover_and_content_pages)
            full_image_template = PageTemplate(id='FullImagePage', frames=[full_frame], onPage=page_drawer.full_image_page_background)
            final_template = PageTemplate(id='FinalPage', frames=[title_frame], onPage=page_drawer.final_page_background)

            doc.addPageTemplates([cover_template, title_template, odd_template, even_template, full_image_template, final_template])

            conversion_status[conversion_id]['progress'] = 60
            conversion_status[conversion_id]['message'] = 'Generating PDF content...'

            # Build story
            story = self.build_story(
                doc, book_title, author_name, book_description, toc_items, content_map,
                image_map, font_size, line_spacing, bool(full_page_image_path),
                frame_width, frame_height
            )

            conversion_status[conversion_id]['progress'] = 80
            conversion_status[conversion_id]['message'] = 'Finalizing PDF...'

            # Build PDF
            doc.build(story)

            # Count pages
            page_count = self.count_pdf_pages(pdf_path)

            conversion_status[conversion_id] = {
                'status': 'completed',
                'progress': 100,
                'message': f'Conversion completed successfully! ({page_count} pages)',
                'pdf_path': pdf_path,
                'book_title': book_title,
                'page_count': page_count,
                'created_at': datetime.now()
            }

        except Exception as e:
            conversion_status[conversion_id] = {
                'status': 'error',
                'progress': 0,
                'message': f'An error occurred: {str(e)}',
                'created_at': datetime.now()
            }


# Flask routes
@converter_bp.route('/convert', methods=['POST'])
def convert_epub():
    try:
        conversion_id = str(uuid.uuid4())
        params = request.get_json()
        
        converter = EpubToPdfConverter()
        thread = threading.Thread(target=converter.convert_epub_to_pdf, args=(conversion_id, params))
        thread.start()
        
        return jsonify({'conversion_id': conversion_id, 'status': 'started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@converter_bp.route('/convert-and-email', methods=['POST'])
def convert_and_email():
    try:
        conversion_id = str(uuid.uuid4())
        data = request.get_json()
        params = data.get('params', {})
        recipient_email = data.get('email')
        
        if not recipient_email:
            return jsonify({'error': 'Email address is required'}), 400
        
        converter = EpubToPdfConverter()
        thread = threading.Thread(target=converter.convert_epub_to_pdf_and_email, args=(conversion_id, params, recipient_email))
        thread.start()
        
        return jsonify({'conversion_id': conversion_id, 'status': 'started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@converter_bp.route('/status/<conversion_id>')
def get_status(conversion_id):
    status = conversion_status.get(conversion_id, {'status': 'not_found'})
    return jsonify(status)

@converter_bp.route('/download/<conversion_id>')
def download_pdf(conversion_id):
    status = conversion_status.get(conversion_id)
    if not status or status['status'] != 'completed':
        return jsonify({'error': 'Conversion not completed or not found'}), 404
    
    pdf_path = status['pdf_path']
    if not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF file not found'}), 404
    
    book_title = status.get('book_title', 'converted_book')
    safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
    
    return send_file(pdf_path, as_attachment=True, download_name=f"{safe_title}.pdf")

# Cleanup old conversions (run periodically)
def cleanup_old_conversions():
    cutoff_time = datetime.now() - timedelta(hours=24)
    to_remove = []
    for conversion_id, status in conversion_status.items():
        if status.get('created_at', datetime.now()) < cutoff_time:
            # Clean up files
            if 'pdf_path' in status and os.path.exists(status['pdf_path']):
                try:
                    os.remove(status['pdf_path'])
                    # Also remove the temp directory if empty
                    temp_dir = os.path.dirname(status['pdf_path'])
                    if os.path.exists(temp_dir) and not os.listdir(temp_dir):
                        os.rmdir(temp_dir)
                except:
                    pass
            to_remove.append(conversion_id)
    
    for conversion_id in to_remove:
        del conversion_status[conversion_id]
