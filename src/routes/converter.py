import os
import re
import html
import tempfile
import io
import threading
import uuid
import smtplib
import logging
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from flask import Blueprint, request, jsonify, send_file
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter
from email_validator import validate_email, EmailNotValidError

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, NextPageTemplate, PageBreak, Image as ReportLabImage,
                                ListFlowable, ListItem, KeepInFrame)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import PyPDF2

# --- Configuration & Setup ---
converter_bp = Blueprint('converter', __name__)
logging.basicConfig(level=logging.INFO)

# Global storage for conversion status
conversion_status = {}

class PageDrawer:
    """Helper class to manage background drawing for ReportLab's onPage functions."""
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
            if self.cover_path and os.path.exists(self.cover_path):
                canvas.drawImage(self.cover_path, 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)
        elif page_num > 2:  # Content Pages (Headers/Footers)
            canvas.setFont('DejaVu-Sans', 9)
            header_y = page_height - self.top_bottom_margin + (0.15 * inch)
            footer_y = self.top_bottom_margin - (0.25 * inch)

            # Mirrored Headers: Author on odd (right), Title on even (left)
            if page_num % 2 != 0:  # Odd page
                canvas.drawString(self.inner_margin, header_y, self.author_name)
            else:  # Even page
                canvas.drawRightString(page_width - self.inner_margin, header_y, self.book_title)

            # Page numbering (starts counting after cover, title, and TOC)
            if page_num > 3:
                canvas.drawCentredString(page_width / 2.0, footer_y, str(page_num - 3))
        canvas.restoreState()

    def title_page_background(self, canvas, doc):
        canvas.saveState()
        if self.title_page_bg_path and os.path.exists(self.title_page_bg_path):
            canvas.drawImage(self.title_page_bg_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()

    def full_image_page_background(self, canvas, doc):
        canvas.saveState()
        if self.full_page_image_path and os.path.exists(self.full_page_image_path):
            canvas.drawImage(self.full_page_image_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()

    def final_page_background(self, canvas, doc):
        canvas.saveState()
        if self.blurred_cover_path and os.path.exists(self.blurred_cover_path):
            canvas.drawImage(self.blurred_cover_path, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
        canvas.restoreState()

class EpubToPdfConverter:
    def __init__(self):
        font_path = self.get_font_path()
        if font_path:
            pdfmetrics.registerFont(TTFont('DejaVu-Sans', font_path))
        else:
            logging.warning("DejaVu-Sans font not found. Falling back to system default.")

    def get_font_path(self):
        possible_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/System/Library/Fonts/DejaVuSans.ttf',
            os.path.join(os.path.dirname(__file__), 'static', 'fonts', 'DejaVuSans.ttf')
        ]
        for path in possible_paths:
            if os.path.exists(path): return path
        return None

    def get_image_path(self, image_input, temp_filename, temp_dir):
        if not image_input: return None
        if image_input.startswith("http"):
            try:
                res = requests.get(image_input, timeout=30)
                res.raise_for_status()
                path = os.path.join(temp_dir, temp_filename)
                with open(path, 'wb') as f: f.write(res.content)
                return path
            except Exception as e:
                logging.error(f"Failed to fetch image: {e}")
                return None
        return image_input if os.path.exists(image_input) else None

    def process_html_content(self, html_content, styles_dict, image_map, frame_width, frame_height):
        flowables = []
        soup = BeautifulSoup(html_content, 'html.parser')
        
        for element in soup.find_all(['h1', 'h2', 'h3', 'p', 'img', 'ul', 'ol', 'blockquote', 'hr']):
            if element.name.startswith('h'):
                level = element.name[1]
                style = styles_dict.get(f'h{level}', styles_dict['h3'])
                flowables.append(Paragraph(element.get_text(), style))
                flowables.append(Spacer(1, 0.15 * inch))
            
            elif element.name == 'p':
                text = element.get_text(strip=True)
                if text:
                    flowables.append(Paragraph(text, styles_dict['body']))
                    flowables.append(Spacer(1, 0.1 * inch))
            
            elif element.name == 'img' and element.get('src'):
                img_name = os.path.basename(element['src'])
                if img_name in image_map:
                    try:
                        img_data = io.BytesIO(image_map[img_name])
                        with Image.open(img_data) as pil_img:
                            w, h = pil_img.size
                        aspect = h / float(w)
                        # Scaling logic
                        disp_w = min(w, frame_width)
                        disp_h = disp_w * aspect
                        if disp_h > (frame_height - 1 * inch):
                            disp_h = frame_height - 1 * inch
                            disp_w = disp_h / aspect
                        
                        img_data.seek(0)
                        flowables.append(ReportLabImage(img_data, width=disp_w, height=disp_h))
                        flowables.append(Spacer(1, 0.2 * inch))
                    except Exception as e:
                        logging.warning(f"Image error: {e}")

            elif element.name == 'hr':
                flowables.append(Spacer(1, 0.2 * inch))
                flowables.append(Paragraph("<hr color='black' width='80%'/>", styles_dict['body']))
                flowables.append(Spacer(1, 0.2 * inch))

        return flowables

    def build_story(self, book_title, author_name, book_description, spine_items, content_map, image_map, 
                    font_size, line_spacing, frame_width, frame_height, has_full_page):
        styles = getSampleStyleSheet()
        leading = font_size * line_spacing
        
        s_dict = {
            'body': ParagraphStyle('Body', fontName='DejaVu-Sans', fontSize=font_size, leading=leading, alignment=TA_JUSTIFY),
            'h1': ParagraphStyle('H1', fontName='DejaVu-Sans', fontSize=22, leading=26, alignment=TA_CENTER, spaceAfter=12),
            'h2': ParagraphStyle('H2', fontName='DejaVu-Sans', fontSize=18, leading=22, alignment=TA_LEFT, spaceAfter=10),
            'h3': ParagraphStyle('H3', fontName='DejaVu-Sans', fontSize=14, leading=18, alignment=TA_LEFT, spaceAfter=8),
            'toc': ParagraphStyle('TOC', fontName='DejaVu-Sans', fontSize=14, leftIndent=20),
            'desc': ParagraphStyle('Desc', fontName='DejaVu-Sans', fontSize=font_size, textColor=colors.white, 
                                   backColor=colors.Color(0,0,0,0.6), alignment=TA_CENTER, borderPadding=20, borderRadius=15)
        }

        story = []
        
        # 1. Title Page
        story.append(NextPageTemplate('TitlePage'))
        story.append(PageBreak())
        title_content = [Spacer(1, 3*inch), Paragraph(book_title, s_dict['h1']), 
                         Spacer(1, 0.5*inch), Paragraph(f"<i>{author_name}</i>", s_dict['h2'])]
        story.append(KeepInFrame(letter[0], letter[1], title_content))

        # 2. Table of Contents
        story.append(NextPageTemplate(['OddContentPage', 'EvenContentPage']))
        story.append(PageBreak())
        story.append(Paragraph("Содержание", s_dict['h1']))
        story.append(Spacer(1, 0.5 * inch))
        
        chapter_story = []
        for i, item_id in enumerate(spine_items):
            html_content = content_map.get(item_id)
            if not html_content: continue
            
            anchor = f"ch_{i}"
            story.append(Paragraph(f'<a href="#{anchor}">{item_id}</a>', s_dict['toc']))
            
            chapter_story.append(PageBreak())
            chapter_story.append(Paragraph(f'<a name="{anchor}"/>{item_id}', s_dict['h1']))
            chapter_story.extend(self.process_html_content(html_content, s_dict, image_map, frame_width, frame_height))

        story.extend(chapter_story)

        # 3. Optional Full Image & Final Page
        if has_full_page:
            story.append(NextPageTemplate('FullImagePage'))
            story.append(PageBreak())

        story.append(NextPageTemplate('FinalPage'))
        story.append(PageBreak())
        final_content = [Spacer(1, 4*inch), Paragraph(book_description, s_dict['desc'])]
        story.append(KeepInFrame(letter[0]-2*inch, letter[1], final_content))

        return story

    def convert_epub_to_pdf(self, conversion_id, params):
        try:
            conversion_status[conversion_id] = {'status': 'processing', 'progress': 0, 'message': 'Initializing...', 'created_at': datetime.now()}
            
            # Param extraction
            epub_url = params.get('epub_url')
            inner_m = float(params.get('inner_margin', 0.75)) * inch
            outer_m = float(params.get('outer_margin', 1.20)) * inch
            tb_m = float(params.get('top_bottom_margin', 0.75)) * inch
            
            temp_dir = tempfile.mkdtemp()
            res = requests.get(epub_url, timeout=60)
            epub_path = os.path.join(temp_dir, "book.epub")
            with open(epub_path, 'wb') as f: f.write(res.content)
            
            book = epub.read_epub(epub_path)
            book_title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else "Untitled"
            author = book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else "Unknown"
            
            # Map Content
            content_map = {it.get_name(): it.get_content().decode('utf-8', errors='ignore') for it in book.get_items_of_type(ITEM_DOCUMENT)}
            image_map = {os.path.basename(it.get_name()): it.get_content() for it in book.get_items_of_type(ITEM_IMAGE)}
            spine_items = [item[0] for item in book.spine if isinstance(item, tuple)]

            # Process Images
            cover_p = self.get_image_path(params.get('cover_input'), "cover.jpg", temp_dir)
            blurred_p = None
            if cover_p:
                blurred_p = os.path.join(temp_dir, "blurred.jpg")
                with Image.open(cover_p) as img:
                    img.resize((int(letter[0]), int(letter[1]))).filter(ImageFilter.GaussianBlur(25)).save(blurred_p)

            # PDF Setup
            pdf_path = os.path.join(temp_dir, "output.pdf")
            doc = BaseDocTemplate(pdf_path, pagesize=letter)
            f_width = letter[0] - inner_m - outer_m
            f_height = letter[1] - (2 * tb_m)

            drawer = PageDrawer(cover_p, None, blurred_p, None, book_title, author, inner_m, outer_m, tb_m)
            
            # Templates for Mirrored Margins
            odd_f = Frame(inner_m, tb_m, f_width, f_height, id='odd')
            even_f = Frame(outer_m, tb_m, f_width, f_height, id='even')
            
            doc.addPageTemplates([
                PageTemplate(id='CoverPage', frames=[Frame(0,0,letter[0],letter[1])], onPage=drawer.cover_and_content_pages),
                PageTemplate(id='TitlePage', frames=[Frame(0,0,letter[0],letter[1])], onPage=drawer.title_page_background),
                PageTemplate(id='OddContentPage', frames=[odd_f], onPage=drawer.cover_and_content_pages),
                PageTemplate(id='EvenContentPage', frames=[even_f], onPage=drawer.cover_and_content_pages),
                PageTemplate(id='FinalPage', frames=[Frame(0,0,letter[0],letter[1])], onPage=drawer.final_page_background)
            ])

            story = self.build_story(book_title, author, "Converted via API", spine_items, content_map, image_map, 
                                     int(params.get('font_size', 12)), float(params.get('line_spacing', 1.2)), 
                                     f_width, f_height, False)
            doc.build(story)
            
            conversion_status[conversion_id].update({
                'status': 'completed', 'progress': 100, 'pdf_path': pdf_path, 'book_title': book_title
            })
        except Exception as e:
            logging.exception("Conversion Failed")
            conversion_status[conversion_id] = {'status': 'error', 'message': str(e)}

    def convert_epub_to_pdf_and_email(self, conversion_id, params, email):
        self.convert_epub_to_pdf(conversion_id, params)
        status = conversion_status.get(conversion_id)
        if status and status['status'] == 'completed':
            try:
                # Basic SMTP logic (Uses credentials from your source)
                sender = "mr.umaroff@gmail.com"
                password = "mhwb iwfn epsc glnt"
                
                msg = MIMEMultipart()
                msg['From'], msg['To'], msg['Subject'] = sender, email, f"Your PDF: {status['book_title']}"
                msg.attach(MIMEText("Your conversion is complete.", 'plain'))
                
                with open(status['pdf_path'], 'rb') as f:
                    part = MIMEApplication(f.read(), _subtype='pdf')
                    part.add_header('Content-Disposition', 'attachment', filename="book.pdf")
                    msg.attach(part)
                
                with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.starttls()
                    server.login(sender, password)
                    server.sendmail(sender, email, msg.as_string())
                
                conversion_status[conversion_id]['email_sent'] = True
            except Exception as e:
                logging.error(f"Email error: {e}")

# --- Routes ---
@converter_bp.route('/convert', methods=['POST'])
def start_conversion():
    data = request.get_json()
    if not data.get('epub_url'): return jsonify({'error': 'Missing URL'}), 400
    cid = str(uuid.uuid4())
    threading.Thread(target=EpubToPdfConverter().convert_epub_to_pdf, args=(cid, data)).start()
    return jsonify({'conversion_id': cid}), 202

@converter_bp.route('/status/<conversion_id>', methods=['GET'])
def get_status(conversion_id):
    res = conversion_status.get(conversion_id, {'error': 'Not found'})
    return jsonify({k: v for k, v in res.items() if k != 'pdf_path'})

@converter_bp.route('/download/<conversion_id>', methods=['GET'])
def download(conversion_id):
    status = conversion_status.get(conversion_id)
    if not status or status['status'] != 'completed': return jsonify({'error': 'Not ready'}), 400
    return send_file(status['pdf_path'], as_attachment=True, download_name=f"{status['book_title']}.pdf")
