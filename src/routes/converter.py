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
                                Spacer, NextPageTemplate, PageBreak, Image as ReportLabImage,
                                Table, TableStyle)
from reportlab.platypus.flowables import KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT
import PyPDF2

converter_bp = Blueprint("converter", __name__)

# Global storage for conversion status
conversion_status = {}

class BookmarkTracker:
    """Helper class to track bookmarks and page numbers during PDF generation"""
    def __init__(self):
        self.bookmarks = {}
        self.current_page = 1
        
    def add_bookmark(self, bookmark_key, page_number):
        self.bookmarks[bookmark_key] = page_number
        
    def get_bookmark_page(self, bookmark_key):
        return self.bookmarks.get(bookmark_key, 1)

class PageDrawer:
    """Helper class to manage data for ReportLab's onPage functions."""
    def __init__(self, cover_path, title_bg_path, blurred_cover_path, full_page_image_path,
                 book_title, author_name, inner_margin, outer_margin, top_bottom_margin, bookmark_tracker=None):
        self.cover_path = cover_path
        self.title_page_bg_path = title_bg_path
        self.blurred_cover_path = blurred_cover_path
        self.full_page_image_path = full_page_image_path
        self.book_title = book_title
        self.author_name = author_name
        self.inner_margin = inner_margin
        self.outer_margin = outer_margin
        self.top_bottom_margin = top_bottom_margin
        self.bookmark_tracker = bookmark_tracker

    def cover_and_content_pages(self, canvas, doc):
        canvas.saveState()
        page_width, page_height = letter
        page_num = canvas.getPageNumber()

        if page_num == 1:  # Cover Page
            if os.path.exists(self.cover_path):
                canvas.drawImage(self.cover_path, 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)
        elif page_num > 2:  # Content Pages (skip title page)
            canvas.setFont("DejaVu-Sans", 9)
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


class ChapterBookmark:
    """Custom flowable to create bookmarks at chapter starts"""
    def __init__(self, bookmark_key, title):
        self.bookmark_key = bookmark_key
        self.title = title
        self.width = 0
        self.height = 0
        
    def draw(self):
        # This flowable doesn't draw anything visible, just creates a bookmark
        canvas = self.canv
        canvas.bookmarkPage(self.bookmark_key)
        canvas.addOutlineEntry(self.title, self.bookmark_key, level=0)


class EpubToPdfConverter:
    def __init__(self):
        # Register DejaVu Sans font (we'll need to include this font file)
        font_path = self.get_font_path()
        if font_path and os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont("DejaVu-Sans", font_path))
        else:
            # Fallback to system fonts
            pdfmetrics.registerFont(TTFont("DejaVu-Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))

    def get_font_path(self):
        """Try to find DejaVu Sans font in common locations"""
        possible_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/DejaVuSans.ttf",
            os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "DejaVuSans.ttf")
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    def count_pdf_pages(self, pdf_path):
        """Count the number of pages in a PDF file"""
        try:
            with open(pdf_path, "rb") as file:
                pdf_reader = PyPDF2.PdfReader(file)
                return len(pdf_reader.pages)
        except Exception as e:
            print(f"Error counting PDF pages: {e}")
            return 0

    def extract_bookmark_page_numbers(self, pdf_path, toc_items):
        """Extract page numbers for each chapter from PDF bookmarks"""
        chapter_pages = {}
        try:
            with open(pdf_path, "rb") as file:
                pdf_reader = PyPDF2.PdfReader(file)
                
                # Get the outline (bookmarks) from the PDF
                if hasattr(pdf_reader, 'outline') and pdf_reader.outline:
                    for i, item in enumerate(toc_items):
                        bookmark_key = f"toc_entry_{i}"
                        
                        # Find the corresponding bookmark in the PDF outline
                        for outline_item in pdf_reader.outline:
                            if hasattr(outline_item, 'title') and outline_item.title == item.title:
                                try:
                                    # Get the page number from the bookmark destination
                                    page_num = pdf_reader.get_destination_page_number(outline_item) + 1
                                    # Adjust for cover and title pages (subtract 2 to get content page number)
                                    content_page = page_num - 2
                                    if content_page > 0:
                                        chapter_pages[bookmark_key] = content_page
                                    break
                                except Exception as e:
                                    print(f"Error getting page for bookmark {item.title}: {e}")
                                    continue
                        
                        # Fallback if bookmark not found
                        if bookmark_key not in chapter_pages:
                            chapter_pages[bookmark_key] = i + 1
                else:
                    # Fallback to sequential numbering if no bookmarks
                    for i, item in enumerate(toc_items):
                        bookmark_key = f"toc_entry_{i}"
                        chapter_pages[bookmark_key] = i + 1
                        
        except Exception as e:
            print(f"Error extracting bookmark page numbers: {e}")
            # Fallback to sequential numbering
            for i, item in enumerate(toc_items):
                bookmark_key = f"toc_entry_{i}"
                chapter_pages[bookmark_key] = i + 1
                
        return chapter_pages

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
            msg["From"] = sender_email
            msg["To"] = recipient_email
            msg["Subject"] = f"Your converted PDF: {book_title}"
            
            # Process email body with custom content or use default
            email_body = self.process_email_body(custom_email_body, book_title, page_count)
            
            msg.attach(MIMEText(email_body, "plain"))
            
            # Attach PDF
            with open(pdf_path, "rb") as f:
                pdf_attachment = MIMEApplication(f.read(), _subtype="pdf")
                safe_title = re.sub(r"[\\/*?:\"<>|]", "", book_title)
                pdf_attachment.add_header("Content-Disposition", "attachment", filename=f"{safe_title}.pdf")
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
                    "status": "error",
                    "progress": 0,
                    "message": "Invalid email address provided",
                    "created_at": datetime.now()
                }
                return

            # First do the regular conversion
            self.convert_epub_to_pdf(conversion_id, params)
            
            # Check if conversion was successful
            if conversion_status[conversion_id]["status"] == "completed":
                conversion_status[conversion_id]["progress"] = 95
                conversion_status[conversion_id]["message"] = "Sending PDF via email..."
                
                pdf_path = conversion_status[conversion_id]["pdf_path"]
                book_title = conversion_status[conversion_id]["book_title"]
                page_count = conversion_status[conversion_id]["page_count"]
                
                # Get custom email body from params
                custom_email_body = params.get("email_body", None)
                
                # Send email with custom body
                if self.send_email_with_pdf(recipient_email, pdf_path, book_title, page_count, custom_email_body):
                    conversion_status[conversion_id]["message"] = f"PDF sent successfully to {recipient_email}! ({page_count} pages)"
                    conversion_status[conversion_id]["progress"] = 100
                    conversion_status[conversion_id]["email_sent"] = True
                    conversion_status[conversion_id]["recipient_email"] = recipient_email
                else:
                    conversion_status[conversion_id]["status"] = "error"
                    conversion_status[conversion_id]["message"] = "PDF generated but failed to send email"
                    
        except Exception as e:
            conversion_status[conversion_id] = {
                "status": "error",
                "progress": 0,
                "message": f"An error occurred: {str(e)}",
                "created_at": datetime.now()
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
                with open(image_path, "wb") as f:
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
                    frame_width, frame_height, toc_page_numbers=None, create_bookmarks=False):
        story = []
        styles = getSampleStyleSheet()
        leading = font_size * line_spacing

        body_style = ParagraphStyle("BodyText", parent=styles["Normal"], fontName="DejaVu-Sans",
                                      fontSize=font_size, leading=leading, alignment=TA_JUSTIFY)
        h1_style = ParagraphStyle("H1", parent=styles["h1"], fontName="DejaVu-Sans",
                                    fontSize=20, leading=24, spaceAfter=12, alignment=TA_CENTER)
        title_page_title_style = ParagraphStyle("TitlePageTitle", parent=styles["h1"], fontName="DejaVu-Sans",
                                                  fontSize=30, textColor=colors.black, alignment=TA_CENTER)
        title_page_author_style = ParagraphStyle("TitlePageAuthor", parent=styles["Normal"], fontName="DejaVu-Sans",
                                                   fontSize=18, textColor=colors.black, alignment=TA_CENTER, spaceBefore=12)
        description_style = ParagraphStyle("Description", parent=body_style, textColor=colors.white,
                                             backColor=colors.Color(0,0,0,0.6), alignment=TA_CENTER,
                                             borderPadding=20, borderRadius=15)

        # Title page
        story.append(NextPageTemplate("TitlePage"))
        story.append(PageBreak())

        title_page_content = [
            Spacer(1, 3*inch),
            Paragraph(book_title, title_page_title_style),
            Spacer(1, 0.25*inch),
            Paragraph(f"<i>{author_name}</i>", title_page_author_style)
        ]
        story.append(KeepInFrame(letter[0], letter[1], title_page_content, vAlign="TOP"))
        
        # Set up alternating page templates for the main content
        story.append(NextPageTemplate(["OddContentPage", "EvenContentPage"]))
        story.append(PageBreak())

        # Table of contents
        story.append(Paragraph("Содержание", h1_style))
        story.append(Spacer(1, 0.25*inch))
        
        if toc_page_numbers:
            # Create TOC with accurate page numbers
            toc_data = []
            toc_title_style = ParagraphStyle("TOCTitle", fontName="DejaVu-Sans", fontSize=14, leading=18, leftIndent=inch*0.25)
            toc_page_style = ParagraphStyle("TOCPage", fontName="DejaVu-Sans", fontSize=14, leading=18, alignment=TA_RIGHT)
            
            for i, item in enumerate(toc_items):
                bookmark_key = f"toc_entry_{i}"
                page_number = toc_page_numbers.get(bookmark_key, 1)
                
                title_cell = Paragraph(f'<a href="#{bookmark_key}">{item.title}</a>', toc_title_style)
                page_cell = Paragraph(str(page_number), toc_page_style)
                
                toc_data.append([title_cell, page_cell])
            
            title_width = frame_width - inch * 1.5
            page_width = inch * 1.5
            
            toc_table = Table(toc_data, colWidths=[title_width, page_width])
            toc_table.setStyle(TableStyle([
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, -1), "DejaVu-Sans"),
                ("FONTSIZE", (0, 0), (-1, -1), 14),
                ("LEADING", (0, 0), (-1, -1), 18),
                ("LEFTPADDING", (0, 0), (0, -1), inch*0.25),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(toc_table)
        else:
            # Simple TOC without page numbers (for first pass)
            toc_style = ParagraphStyle("TOC", parent=styles["Normal"], fontName="DejaVu-Sans",
                                         fontSize=14, leading=18, leftIndent=inch*0.25)
            for i, item in enumerate(toc_items):
                bookmark_key = f"toc_entry_{i}"
                story.append(Paragraph(f'<a href="#{bookmark_key}">{item.title}</a>', toc_style))
        
        story.append(Spacer(1, 0.5*inch))

        # Chapter content
        for i, item in enumerate(toc_items):
            bookmark_key = f"toc_entry_{i}"
            story.append(PageBreak())
            
            # Add bookmark for first pass
            if create_bookmarks:
                story.append(ChapterBookmark(bookmark_key, item.title))
            
            title_with_anchor = f'<a name="{bookmark_key}"/>{item.title}'
            story.append(Paragraph(title_with_anchor, h1_style))

            chapter_html = content_map.get(item.href.split("#")[0])
            if chapter_html:
                soup = BeautifulSoup(chapter_html, "html.parser")
                for tag in soup.find_all(["p", "img"]):
                    if tag.name == "p" and tag.get_text(strip=True):
                        story.append(Paragraph(tag.get_text(strip=True), body_style))
                        story.append(Spacer(1, 0.1 * inch))
                    elif tag.name == "img" and tag.get("src"):
                        img_src_base = os.path.basename(tag["src"])
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
                                story.append(rl_image)
                                story.append(Spacer(1, 0.2 * inch))
                            except Exception:
                                pass  # Skip problematic images

        if has_full_page_image:
            story.append(NextPageTemplate("FullImagePage"))
            story.append(PageBreak())

        # Final page
        story.append(NextPageTemplate("FinalPage"))
        story.append(PageBreak())
        final_page_content = [
            Spacer(1, (letter[1] / 2) - 2*inch),
            Paragraph(book_description, description_style)
        ]
        story.append(KeepInFrame(letter[0] - 2*inch, letter[1], final_page_content, hAlign="CENTER", vAlign="MIDDLE"))

        return story

    def cleanup_temp_files(self, file_paths, temp_dir):
        for path in file_paths:
            if path and path.startswith(temp_dir) and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def convert_epub_to_pdf(self, conversion_id, params):
        """Main conversion logic with two-pass PDF generation for accurate TOC using bookmarks"""
        try:
            conversion_status[conversion_id] = {
                "status": "processing",
                "progress": 0,
                "message": "Starting conversion...",
                "created_at": datetime.now()
            }

            # Extract parameters
            epub_url = params.get("epub_url")
            cover_input = params.get("cover_input", "")
            title_page_bg_input = params.get("title_page_bg_input", "")
            full_page_image_input = params.get("full_page_image_input", "")
            font_size = int(params.get("font_size", 13))
            line_spacing = float(params.get("line_spacing", 1.5))
            
            inner_margin = float(params.get("inner_margin", 1.25)) * inch
            outer_margin = float(params.get("outer_margin", 0.75)) * inch
            top_bottom_margin = float(params.get("top_bottom_margin", 1.0)) * inch

            conversion_status[conversion_id]["progress"] = 5
            conversion_status[conversion_id]["message"] = "Fetching EPUB file..."

            # Fetch EPUB
            response = requests.get(epub_url, timeout=60)
            response.raise_for_status()

            temp_dir = tempfile.mkdtemp()
            epub_path = os.path.join(temp_dir, "book.epub")
            with open(epub_path, "wb") as f:
                f.write(response.content)
            book = epub.read_epub(epub_path)

            conversion_status[conversion_id]["progress"] = 15
            conversion_status[conversion_id]["message"] = "Processing EPUB content..."

            # Extract metadata
            book_title, author_name, book_description = "Unknown Title", "Unknown Author", "No description found."
            if book.get_metadata("DC", "title"): book_title = book.get_metadata("DC", "title")[0][0]
            if book.get_metadata("DC", "creator"): author_name = book.get_metadata("DC", "creator")[0][0]
            if book.get_metadata("DC", "description"): 
                raw_desc = book.get_metadata("DC", "description")[0][0]
                book_description = html.unescape(re.sub("<[^<]+?>", "", raw_desc))

            # Map content and images
            toc_items = self.flatten_toc(book.toc)
            content_map = {item.get_name(): item.get_content() for item in book.get_items_of_type(ITEM_DOCUMENT)}
            image_map = {os.path.basename(item.get_name()): item.get_content() for item in book.get_items_of_type(ITEM_IMAGE)}

            conversion_status[conversion_id]["progress"] = 25
            conversion_status[conversion_id]["message"] = "Preparing images..."

            # Process images
            cover_path = self.get_image_path(cover_input, "cover.jpg", temp_dir) if cover_input else None
            title_bg_path = self.get_image_path(title_page_bg_input, "title_bg.jpg", temp_dir) if title_page_bg_input else None
            full_page_image_path = self.get_image_path(full_page_image_input, "full_page_image.jpg", temp_dir) if full_page_image_input else None

            blurred_cover_path = None
            if cover_path and os.path.exists(cover_path):
                blurred_cover_path = os.path.join(temp_dir, "blurred_cover.jpg")
                with Image.open(cover_path) as img:
                    img_resized = img.resize((int(letter[0]), int(letter[1])))
                    img_resized.filter(ImageFilter.GaussianBlur(25)).save(blurred_cover_path)

            # --- First Pass: Generate PDF with bookmarks to get page numbers ---
            conversion_status[conversion_id]["progress"] = 35
            conversion_status[conversion_id]["message"] = "First pass: Generating content with bookmarks..."

            first_pass_filename = os.path.join(temp_dir, "first_pass.pdf")
            doc_first_pass = BaseDocTemplate(first_pass_filename, pagesize=letter)
            
            page_width, page_height = letter
            frame_width = page_width - inner_margin - outer_margin
            frame_height = page_height - (2 * top_bottom_margin)

            bookmark_tracker = BookmarkTracker()
            page_drawer = PageDrawer(cover_path or "", title_bg_path or "", blurred_cover_path or "", full_page_image_path, book_title, author_name, inner_margin, outer_margin, top_bottom_margin, bookmark_tracker)
            
            odd_frame = Frame(inner_margin, top_bottom_margin, frame_width, frame_height, id="odd_frame")
            even_frame = Frame(outer_margin, top_bottom_margin, frame_width, frame_height, id="even_frame")

            page_templates = [
                PageTemplate(id="CoverPage", frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id="TitlePage", frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.title_page_background),
                PageTemplate(id="OddContentPage", frames=[odd_frame], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id="EvenContentPage", frames=[even_frame], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id="FinalPage", frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.final_page_background)
            ]
            if full_page_image_path: page_templates.append(PageTemplate(id="FullImagePage", frames=[Frame(0, 0, letter[0], letter[1])], onPage=page_drawer.full_image_page_background))

            doc_first_pass.addPageTemplates(page_templates)

            # Build story with bookmarks
            story_first_pass = self.build_story(doc_first_pass, book_title, author_name, book_description, toc_items, content_map, image_map, font_size, line_spacing, bool(full_page_image_path), frame_width, frame_height, create_bookmarks=True)
            doc_first_pass.build(story_first_pass)

            # --- Extract page numbers from the first pass PDF bookmarks ---
            conversion_status[conversion_id]["progress"] = 65
            conversion_status[conversion_id]["message"] = "Extracting accurate page numbers from bookmarks..."
            
            toc_page_numbers = self.extract_bookmark_page_numbers(first_pass_filename, toc_items)

            # --- Second Pass: Generate final PDF with accurate TOC ---
            conversion_status[conversion_id]["progress"] = 75
            conversion_status[conversion_id]["message"] = "Second pass: Generating final PDF with accurate TOC..."

            safe_title = re.sub(r'[\\/*?:\"<>|]', '', book_title)
            final_pdf_filename = os.path.join(temp_dir, f"{safe_title}.pdf")
            doc_final = BaseDocTemplate(final_pdf_filename, pagesize=letter)
            doc_final.addPageTemplates(page_templates)

            # Build story with accurate TOC
            story_final = self.build_story(doc_final, book_title, author_name, book_description, toc_items, content_map, image_map, font_size, line_spacing, bool(full_page_image_path), frame_width, frame_height, toc_page_numbers=toc_page_numbers)
            doc_final.build(story_final)

            conversion_status[conversion_id]["progress"] = 95
            conversion_status[conversion_id]["message"] = "Finalizing and counting pages..."

            page_count = self.count_pdf_pages(final_pdf_filename)

            # Cleanup temp files
            files_to_clean = [epub_path, cover_path, title_bg_path, blurred_cover_path, full_page_image_path, first_pass_filename]
            self.cleanup_temp_files(files_to_clean, temp_dir)

            conversion_status[conversion_id] = {
                "status": "completed",
                "progress": 100,
                "message": f"PDF generation complete! ({page_count} pages)",
                "pdf_path": final_pdf_filename,
                "book_title": book_title,
                "page_count": page_count,
                "created_at": datetime.now()
            }

        except Exception as e:
            conversion_status[conversion_id] = {
                "status": "error",
                "progress": 0,
                "message": f"An error occurred: {str(e)}",
                "created_at": datetime.now()
            }


@converter_bp.route("/convert-and-email", methods=["POST"])
def start_conversion_and_email():
    """Start EPUB to PDF conversion and send via email"""
    try:
        data = request.get_json()
        if not data or not data.get("epub_url"): return jsonify({"error": "EPUB URL is required"}), 400
        if not data.get("email"): return jsonify({"error": "Email address is required"}), 400

        conversion_id = str(uuid.uuid4())
        recipient_email = data.get("email")

        converter = EpubToPdfConverter()
        thread = threading.Thread(target=converter.convert_epub_to_pdf_and_email, args=(conversion_id, data, recipient_email))
        thread.daemon = True
        thread.start()

        return jsonify({"conversion_id": conversion_id, "message": f"Conversion started, PDF will be sent to {recipient_email}"}), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@converter_bp.route("/convert", methods=["POST"])
def start_conversion():
    """Start EPUB to PDF conversion"""
    try:
        data = request.get_json()
        if not data or not data.get("epub_url"): return jsonify({"error": "EPUB URL is required"}), 400

        conversion_id = str(uuid.uuid4())

        converter = EpubToPdfConverter()
        thread = threading.Thread(target=converter.convert_epub_to_pdf, args=(conversion_id, data))
        thread.daemon = True
        thread.start()

        return jsonify({"conversion_id": conversion_id, "message": "Conversion started successfully"}), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@converter_bp.route("/status/<conversion_id>", methods=["GET"])
def get_conversion_status(conversion_id):
    """Get conversion status"""
    if conversion_id not in conversion_status: return jsonify({"error": "Conversion not found"}), 404

    status = conversion_status[conversion_id].copy()
    if "pdf_path" in status: del status["pdf_path"]

    return jsonify(status)


@converter_bp.route("/download/<conversion_id>", methods=["GET"])
def download_pdf(conversion_id):
    """Download generated PDF"""
    if conversion_id not in conversion_status: return jsonify({"error": "Conversion not found"}), 404

    status = conversion_status[conversion_id]
    if status["status"] != "completed": return jsonify({"error": "Conversion not completed"}), 400

    pdf_path = status.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path): return jsonify({"error": "PDF file not found"}), 404

    book_title = status.get("book_title", "converted_book")
    safe_title = re.sub(r"[\\/*?:\"<>|]", "", book_title)

    return send_file(pdf_path, as_attachment=True, download_name=f"{safe_title}.pdf", mimetype="application/pdf")


def cleanup_old_conversions():
    """Remove conversion records older than 1 hour"""
    cutoff_time = datetime.now() - timedelta(hours=1)
    to_remove = []

    for conv_id, status in conversion_status.items():
        if status.get("created_at", datetime.now()) < cutoff_time:
            if "pdf_path" in status and os.path.exists(status["pdf_path"]):
                try:
                    os.remove(status["pdf_path"])
                    temp_dir = os.path.dirname(status["pdf_path"])
                    if os.path.exists(temp_dir): os.rmdir(temp_dir)
                except OSError:
                    pass
            to_remove.append(conv_id)

    for conv_id in to_remove: del conversion_status[conv_id]


@converter_bp.route("/cleanup", methods=["POST"])
def manual_cleanup():
    """Manual cleanup endpoint for old conversions"""
    cleanup_old_conversions()
    return jsonify({"message": "Cleanup completed"})
