import os
import sys
import requests
import re
import html
import tempfile
import io
import threading
import uuid
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

converter_bp = Blueprint('converter', __name__)

# Global storage for conversion status
conversion_status = {}

class PageDrawer:
    """Helper class to manage data for ReportLab's onPage functions."""
    def __init__(self, cover_path, title_bg_path, blurred_cover_path, full_page_image_path, 
                 book_title, author_name, left_margin, right_margin, top_margin, bottom_margin):
        self.cover_path = cover_path
        self.title_page_bg_path = title_bg_path
        self.blurred_cover_path = blurred_cover_path
        self.full_page_image_path = full_page_image_path
        self.book_title = book_title
        self.author_name = author_name
        self.left_margin = left_margin
        self.right_margin = right_margin
        self.top_margin = top_margin
        self.bottom_margin = bottom_margin

    def cover_and_content_pages(self, canvas, doc):
        canvas.saveState()
        page_width, page_height = letter
        page_num = canvas.getPageNumber()

        if page_num == 1:  # Cover Page
            if os.path.exists(self.cover_path):
                canvas.drawImage(self.cover_path, 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)
        elif page_num > 2:  # Content Pages (skip title page)
            canvas.setFont('DejaVu-Sans', 9)
            header_y = page_height - self.top_margin + inch * 0.15
            
            # Alternating alignment starting with left on page 3
            if page_num % 2 != 0:  # Odd pages (3, 5, ...) are left-aligned
                canvas.drawString(self.left_margin, header_y, self.author_name)
            else:  # Even pages (4, 6, ...) are right-aligned
                canvas.drawRightString(page_width - self.right_margin, header_y, self.book_title)
            
            if page_num > 3:
                canvas.drawCentredString(page_width / 2.0, self.bottom_margin - inch * 0.25, str(page_num - 3))
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

    def build_story(self, doc, book_title, author_name, book_description, toc_items, content_map, 
                   image_map, font_size, line_spacing, has_full_page_image):
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
        story.append(NextPageTemplate('ContentPage'))
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
                                max_width = doc.width
                                max_height = doc.height - V_BUFFER

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
            margin_size = float(params.get('margin_size', 1.0))

            # Update progress
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
            
            conversion_status[conversion_id]['progress'] = 35
            conversion_status[conversion_id]['message'] = 'Building PDF structure...'

            # Build PDF
            safe_title = re.sub(r'[\\/*?:"<>|]', "", book_title)
            pdf_filename = os.path.join(temp_dir, f"{safe_title}.pdf")

            doc = BaseDocTemplate(pdf_filename, pagesize=letter)
            margin = margin_size * inch
            doc.leftMargin, doc.rightMargin, doc.topMargin, doc.bottomMargin = margin, margin, margin, margin

            page_drawer = PageDrawer(
                cover_path=cover_path or '', title_bg_path=title_bg_path or '', 
                blurred_cover_path=blurred_cover_path or '', 
                full_page_image_path=full_page_image_path,
                book_title=book_title, author_name=author_name,
                left_margin=doc.leftMargin, right_margin=doc.rightMargin,
                top_margin=doc.topMargin, bottom_margin=doc.bottomMargin
            )

            page_templates = [
                PageTemplate(id='CoverPage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id='TitlePage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.title_page_background),
                PageTemplate(id='ContentPage', frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height)], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id='FinalPage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.final_page_background)
            ]
            
            if full_page_image_path:
                page_templates.append(PageTemplate(id='FullImagePage', frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.full_image_page_background))

            doc.addPageTemplates(page_templates)
            
            conversion_status[conversion_id]['progress'] = 45
            conversion_status[conversion_id]['message'] = 'Assembling document content...'

            # Build story
            story = self.build_story(doc, book_title, author_name, book_description, toc_items, 
                                   content_map, image_map, font_size, line_spacing, bool(full_page_image_path))
            
            conversion_status[conversion_id]['progress'] = 85
            conversion_status[conversion_id]['message'] = 'Generating PDF...'

            # Generate PDF
            doc.build(story)
            
            # Cleanup temp files except the final PDF
            files_to_clean = [epub_path, cover_path, title_bg_path, blurred_cover_path, full_page_image_path]
            self.cleanup_temp_files(files_to_clean, temp_dir)

            conversion_status[conversion_id] = {
                'status': 'completed',
                'progress': 100,
                'message': 'PDF generation complete!',
                'pdf_path': pdf_filename,
                'book_title': book_title,
                'created_at': datetime.now()
            }

        except Exception as e:
            conversion_status[conversion_id] = {
                'status': 'error',
                'progress': 0,
                'message': f'An error occurred: {str(e)}',
                'created_at': datetime.now()
            }


@converter_bp.route('/convert', methods=['POST'])
def start_conversion():
    """Start EPUB to PDF conversion"""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data or not data.get('epub_url'):
            return jsonify({'error': 'EPUB URL is required'}), 400
        
        # Generate unique conversion ID
        conversion_id = str(uuid.uuid4())
        
        # Start conversion in background thread
        converter = EpubToPdfConverter()
        thread = threading.Thread(
            target=converter.convert_epub_to_pdf,
            args=(conversion_id, data)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'conversion_id': conversion_id,
            'message': 'Conversion started successfully'
        }), 202
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@converter_bp.route('/status/<conversion_id>', methods=['GET'])
def get_conversion_status(conversion_id):
    """Get conversion status"""
    if conversion_id not in conversion_status:
        return jsonify({'error': 'Conversion not found'}), 404
    
    status = conversion_status[conversion_id].copy()
    # Remove file path from response for security
    if 'pdf_path' in status:
        del status['pdf_path']
    
    return jsonify(status)


@converter_bp.route('/download/<conversion_id>', methods=['GET'])
def download_pdf(conversion_id):
    """Download generated PDF"""
    if conversion_id not in conversion_status:
        return jsonify({'error': 'Conversion not found'}), 404
    
    status = conversion_status[conversion_id]
    if status['status'] != 'completed':
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

