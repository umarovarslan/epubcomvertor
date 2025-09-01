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
import zipfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email_validator import validate_email, EmailNotValidError
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_file
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE, ITEM_STYLE
from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, NextPageTemplate, PageBreak, Image as ReportLabImage,
                                ListFlowable, ListItem)
from reportlab.platypus.flowables import KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import PyPDF2
import logging

converter_bp = Blueprint('converter', __name__)
logging.basicConfig(level=logging.INFO)

# Global storage for conversion status
conversion_status = {}

def patch_epub_container(epub_path, temp_dir):
    container_xml_path = 'META-INF/container.xml'
    temp_epub_path = os.path.join(temp_dir, os.path.basename(epub_path))

    # Create a new zip file to copy contents and add the missing file
    with zipfile.ZipFile(epub_path, 'r') as original_epub:
        with zipfile.ZipFile(temp_epub_path, 'w', zipfile.ZIP_DEFLATED) as new_epub:
            for item in original_epub.infolist():
                # Copy all existing files
                new_epub.writestr(item, original_epub.read(item.filename))
            
            # Add the dummy container.xml if it's missing
            if container_xml_path not in [item.filename for item in original_epub.infolist()]:
                dummy_container_content = """
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
                new_epub.writestr(container_xml_path, dummy_container_content.strip())

    # Replace the original epub with the patched one
    os.replace(temp_epub_path, epub_path)
    logging.info(f"Patched EPUB saved to {epub_path}")

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
        # Register DejaVu Sans font
        font_path = self.get_font_path()
        if font_path and os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont('DejaVu-Sans', font_path))
        else:
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
            logging.error(f"Error counting PDF pages: {e}")
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
            # Email configuration
            smtp_server = "smtp.gmail.com"
            smtp_port = 587
            sender_email = "mr.umaroff@gmail.com"
            sender_password = "mhwb iwfn epsc glnt"
            
            # Create message
            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = recipient_email
            msg['Subject'] = f"Your converted PDF: {book_title}"
            
            # Process email body
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
            logging.error(f"Error sending email: {e}")
            return False

    def process_html_content(self, html_content, body_style, h1_style, h2_style, h3_style, image_map, frame_width, frame_height):
        """Process HTML content and convert to ReportLab flowables"""
        flowables = []
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Process all elements in order
        for element in soup.find_all(True):
            if element.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                # Handle headings
                level = int(element.name[1])
                text = element.get_text(strip=True)
                if text:
                    if level == 1:
                        flowables.append(Paragraph(text, h1_style))
                    elif level == 2:
                        flowables.append(Paragraph(text, h2_style))
                    else:
                        flowables.append(Paragraph(text, h3_style))
                    flowables.append(Spacer(1, 0.15 * inch))
            
            elif element.name == 'p':
                # Handle paragraphs
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(text, body_style))
                    flowables.append(Spacer(1, 0.1 * inch))
            
            elif element.name == 'img' and element.get('src'):
                # Handle images
                img_src_base = os.path.basename(element['src'])
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
                        flowables.append(rl_image)
                        flowables.append(Spacer(1, 0.2 * inch))
                    except Exception as e:
                        logging.warning(f"Error processing image: {e}")
            
            elif element.name == 'div':
                # Handle divs by recursively processing their content
                div_flowables = self.process_html_content(str(element), body_style, h1_style, h2_style, h3_style, image_map, frame_width, frame_height)
                flowables.extend(div_flowables)
            
            elif element.name in ['strong', 'b']:
                # Handle bold text
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(f"<b>{text}</b>", body_style))
            
            elif element.name in ['em', 'i']:
                # Handle italic text
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(f"<i>{text}</i>", body_style))
            
            elif element.name == 'br':
                # Handle line breaks
                flowables.append(Spacer(1, 0.1 * inch))
            
            elif element.name == 'hr':
                # Handle horizontal rules
                flowables.append(Spacer(1, 0.3 * inch))
                flowables.append(Paragraph("―" * 50, body_style))
                flowables.append(Spacer(1, 0.3 * inch))
            
            elif element.name == 'blockquote':
                # Handle blockquotes
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(f'<para leftIndent="20" spaceAfter="10"><i>"{text}"</i></para>', body_style))
            
            elif element.name in ['ul', 'ol']:
                # Handle lists
                list_items = []
                for li in element.find_all('li', recursive=False):
                    item_text = li.get_text(strip=True)
                    if item_text:
                        list_items.append(ListItem(Paragraph(item_text, body_style), leftIndent=20))
                
                if list_items:
                    bullet_style = 'bullet' if element.name == 'ul' else '1'
                    flowables.append(ListFlowable(list_items, bulletType=bullet_style))
                    flowables.append(Spacer(1, 0.2 * inch))
            
            else:
                # Fallback for other elements
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(text, body_style))
        
        return flowables

    def build_story(self, doc, book_title, author_name, book_description, spine_items, content_map, image_map,
                    font_size, line_spacing, has_full_page_image, frame_width, frame_height):
        """Build the PDF story from EPUB content"""
        story = []
        styles = getSampleStyleSheet()
        leading = font_size * line_spacing

        # Define styles
        body_style = ParagraphStyle('BodyText', parent=styles['Normal'], fontName='DejaVu-Sans',
                                    fontSize=font_size, leading=leading, alignment=TA_JUSTIFY)
        h1_style = ParagraphStyle('H1', parent=styles['h1'], fontName='DejaVu-Sans',
                                  fontSize=20, leading=24, spaceAfter=12, alignment=TA_CENTER)
        h2_style = ParagraphStyle('H2', parent=styles['h2'], fontName='DejaVu-Sans',
                                  fontSize=16, leading=20, spaceAfter=10, alignment=TA_LEFT)
        h3_style = ParagraphStyle('H3', parent=styles['h3'], fontName='DejaVu-Sans',
                                  fontSize=14, leading=18, spaceAfter=8, alignment=TA_LEFT)
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
        
        # Set up alternating page templates for main content
        story.append(NextPageTemplate(['OddContentPage', 'EvenContentPage']))
        story.append(PageBreak())

        # Table of contents
        toc_page_content = [Paragraph("Содержание", h1_style), Spacer(1, 0.25*inch)]
        chapter_content_story = []
        toc_links = []

        # Process each spine item in order
        for i, item_id in enumerate(spine_items):
            content_html = content_map.get(item_id)
            if not content_html:
                continue

            # Create anchor for TOC
            anchor_key = f'chapter_{i}'
            toc_links.append((item_id, anchor_key))
            
            # Add chapter title to TOC
            toc_page_content.append(Paragraph(f'<a href="#{anchor_key}">{item_id}</a>', toc_style))
            
            # Process chapter content
            chapter_content_story.append(PageBreak())
            chapter_content_story.append(Paragraph(f'<a name="{anchor_key}"/>', body_style))
            chapter_flowables = self.process_html_content(content_html.decode('utf-8'), body_style, h1_style, h2_style, h3_style, image_map, frame_width, frame_height)
            chapter_content_story.extend(chapter_flowables)

        story.extend(toc_page_content)
        story.extend(chapter_content_story)

        if has_full_page_image:
            story.append(NextPageTemplate('FullImagePage'))
            story.append(PageBreak())

        # Final page
        story.append(NextPageTemplate('FinalPage'))
        story.append(PageBreak())
        final_page_content = [
            Spacer(1, (letter[1] / 2) - 2*inch),
            Paragraph(book_description, description_style)
        ]
        story.append(KeepInFrame(letter[0] - 2*inch, letter[1], final_page_content, hAlign='CENTER', vAlign='MIDDLE'))

        return story

    def cleanup_temp_files(self, file_paths, temp_dir):
        for path in file_paths:
            if path and path.startswith(temp_dir) and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

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

            # Patch the EPUB file if container.xml is missing
            try:
                patch_epub_container(epub_path, temp_dir)
            except Exception as patch_e:
                logging.warning(f"Could not patch EPUB: {patch_e}")

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
            spine_items = [item.href for item in book.spine]
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
                    img_blurred = img_resized.filter(ImageFilter.GaussianBlur(10))
                    img_blurred.save(blurred_cover_path)

            conversion_status[conversion_id]['progress'] = 35
            conversion_status[conversion_id]['message'] = 'Setting up PDF document...'

            # Setup PDF document
            pdf_filename = os.path.join(temp_dir, f"{conversion_id}.pdf")
            doc = BaseDocTemplate(pdf_filename, pagesize=letter)
            frame_width = letter[0] - inner_margin - outer_margin
            frame_height = letter[1] - 2 * top_bottom_margin

            page_drawer = PageDrawer(
                cover_path=cover_path or '', title_bg_path=title_bg_path or '',
                blurred_cover_path=blurred_cover_path or '',
                full_page_image_path=full_page_image_path,
                book_title=book_title, author_name=author_name,
                inner_margin=inner_margin, outer_margin=outer_margin,
                top_bottom_margin=top_bottom_margin
            )
            
            # Define frames and page templates
            odd_frame = Frame(inner_margin, top_bottom_margin, frame_width, frame_height, id='odd_frame')
            even_frame = Frame(outer_margin, top_bottom_margin, frame_width, frame_height, id='even_frame')

            page_templates = [
                PageTemplate(id='CoverPage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id='TitlePage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.title_page_background),
                PageTemplate(id='OddContentPage', frames=[odd_frame], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id='EvenContentPage', frames=[even_frame], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id='FinalPage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.final_page_background)
            ]

            if full_page_image_path:
                page_templates.append(PageTemplate(id='FullImagePage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.full_image_page_background))

            doc.addPageTemplates(page_templates)

            conversion_status[conversion_id]['progress'] = 45
            conversion_status[conversion_id]['message'] = 'Assembling document content...'

            # Build story
            story = self.build_story(doc, book_title, author_name, book_description, spine_items,
                                     content_map, image_map, font_size, line_spacing, bool(full_page_image_path),
                                     frame_width, frame_height)

            conversion_status[conversion_id]['progress'] = 85
            conversion_status[conversion_id]['message'] = 'Generating PDF...'

            # Generate PDF
            doc.build(story)

            conversion_status[conversion_id]['progress'] = 95
            conversion_status[conversion_id]['message'] = 'Counting PDF pages...'

            # Count PDF pages
            page_count = self.count_pdf_pages(pdf_filename)

            # Cleanup temp files except the final PDF
            files_to_clean = [epub_path, cover_path, title_bg_path, blurred_cover_path, full_page_image_path]
            self.cleanup_temp_files(files_to_clean, temp_dir)

            conversion_status[conversion_id] = {
                'status': 'completed',
                'progress': 100,
                'message': f'PDF generation complete! ({page_count} pages)',
                'pdf_path': pdf_filename,
                'book_title': book_title,
                'page_count': page_count,
                'created_at': datetime.now()
            }

        except Exception as e:
            logging.exception("Conversion error")
            conversion_status[conversion_id] = {
                'status': 'error',
                'progress': 0,
                'message': f'An error occurred: {str(e)}',
                'created_at': datetime.now()
            }

    def convert_epub_to_pdf_and_email(self, conversion_id, params, recipient_email):
        """Convert EPUB to PDF and send via email"""
        try:
            # Validate email
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
            logging.exception("Email sending error")
            conversion_status[conversion_id] = {
                'status': 'error',
                'progress': 0,
                'message': f'An error occurred: {str(e)}',
                'created_at': datetime.now()
            }
    def flatten_toc(self, toc_list):
        flat_list = []
        for item in toc_list:
            if isinstance(item, (list, tuple)):
                flat_list.extend(self.flatten_toc(item))
            elif isinstance(item, epub.Link):
                flat_list.append(item)
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

@converter_bp.route('/start-conversion', methods=['POST'])
def start_conversion():
    """Start a new EPUB to PDF conversion"""
    params = request.json
    if not params or 'epub_url' not in params:
        return jsonify({'error': 'Missing epub_url in request'}), 400

    conversion_id = str(uuid.uuid4())
    recipient_email = params.get('recipient_email')

    converter = EpubToPdfConverter()
    
    if recipient_email:
        # Run conversion and email sending in a background thread
        thread = threading.Thread(target=converter.convert_epub_to_pdf_and_email, args=(conversion_id, params, recipient_email))
    else:
        # Run only the conversion in a background thread
        thread = threading.Thread(target=converter.convert_epub_to_pdf, args=(conversion_id, params))
    
    thread.start()

    return jsonify({'conversion_id': conversion_id})

@converter_bp.route('/conversion-status/<conversion_id>', methods=['GET'])
def get_conversion_status(conversion_id):
    """Get the status of a conversion"""
    status = conversion_status.get(conversion_id)
    if not status:
        return jsonify({'error': 'Conversion ID not found'}), 404
    return jsonify(status)

@converter_bp.route('/download-pdf/<conversion_id>', methods=['GET'])
def download_pdf(conversion_id):
    """Download the converted PDF"""
    status = conversion_status.get(conversion_id)
    if not status:
        return jsonify({'error': 'Conversion ID not found'}), 404

    if status.get('status') != 'completed':
        return jsonify({'error': 'Conversion not completed'}), 400

    pdf_path = status.get('pdf_path')
    if not pdf_path or not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF file not found'}), 404

    book_title = status.get('book_title', 'converted_book')
    safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)

    return send_file(
        pdf_path,
        as_attachment=True,
        download_name=f"{safe_title}.pdf",
        mimetype='application/pdf'
    )


# Cleanup old conversions periodically (simple implementation)
def cleanup_old_conversions():
    """Remove conversion records older than 1 hour"""
    cutoff_time = datetime.now() - timedelta(hours=1)
    to_remove = []

    for conv_id, status in conversion_status.items():
        if status.get('created_at', datetime.now()) < cutoff_time:
            # Also cleanup the PDF file if it exists
            if 'pdf_path' in status and os.path.exists(status['pdf_path']):
                try:
                    os.remove(status['pdf_path'])
                    # Try to remove the temp directory if empty
                    temp_dir = os.path.dirname(status['pdf_path'])
                    if os.path.exists(temp_dir):
                        os.rmdir(temp_dir)
                except OSError:
                    pass
            to_remove.append(conv_id)

    for conv_id in to_remove:
        del conversion_status[conv_id]


@converter_bp.route('/cleanup', methods=['POST'])
def manual_cleanup():
    """Manual cleanup endpoint for old conversions"""
    cleanup_old_conversions()
    return jsonify({'message': 'Cleanup completed'})
