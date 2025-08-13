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
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE, ITEM_STYLE
from bs4 import BeautifulSoup, Tag, NavigableString
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, NextPageTemplate, PageBreak, Image as ReportLabImage,
                                ListFlowable, ListItem, KeepInFrame, Flowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import PyPDF2
import logging

converter_bp = Blueprint("converter", __name__)
logging.basicConfig(level=logging.INFO)

# Global storage for conversion status
conversion_status = {}

class PageDrawer:
    """Helper class to manage data for ReportLab\"s onPage functions."""
    def __init__(self, cover_path, title_bg_path, blurred_cover_path, full_page_image_path,
                 book_title, author_name, inner_margin, outer_margin, top_bottom_margin):
        self.cover_path = cover_path
        self.title_page_bg = title_bg_path
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
            canvas.setFont("DejaVu-Sans", 9)
            header_y = page_height - self.top_bottom_margin + inch * 0.15

            # Headers are on the inner top margin
            # Simplified to single content page template, so no alternating margins
            canvas.drawString(self.inner_margin, header_y, self.author_name)
            canvas.drawRightString(page_width - self.inner_margin, header_y, self.book_title)

            if page_num > 3: # Page numbering starts after TOC
                canvas.drawCentredString(page_width / 2.0, self.top_bottom_margin - inch * 0.25, str(page_num - 3))
        canvas.restoreState()

    def title_page_background(self, canvas, doc):
        canvas.saveState()
        if os.path.exists(self.title_page_bg):
            canvas.drawImage(self.title_page_bg, 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
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
        if font_path:
            pdfmetrics.registerFont(TTFont("DejaVu-Sans", font_path))
            
            # Try to register bold, italic, and bold-italic variations
            # Use Oblique for italic if Italic.ttf is not found
            bold_path = font_path.replace(".ttf", "-Bold.ttf").replace(".otf", "-Bold.otf")
            if not os.path.exists(bold_path):
                bold_path = font_path # Fallback
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-Bold", bold_path))

            italic_path = font_path.replace(".ttf", "-Oblique.ttf").replace(".otf", "-Oblique.otf") # Corrected for Oblique
            if not os.path.exists(italic_path):
                italic_path = font_path # Fallback
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-Italic", italic_path))

            bold_italic_path = font_path.replace(".ttf", "-BoldOblique.ttf").replace(".otf", "-BoldOblique.otf") # Corrected for BoldOblique
            if not os.path.exists(bold_italic_path):
                bold_italic_path = font_path # Fallback
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-BoldItalic", bold_italic_path))
        else:
            # Fallback to default system font paths if no DejaVu Sans is found
            logging.warning("DejaVuSans.ttf not found in common locations. Using system fallbacks.")
            pdfmetrics.registerFont(TTFont("DejaVu-Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-Italic", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"))
            pdfmetrics.registerFont(TTFont("DejaVu-Sans-BoldItalic", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"))

    def get_font_path(self):
        """Try to find DejaVu Sans font in common locations."""
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
        """Count the number of pages in a PDF file."""
        try:
            with open(pdf_path, "rb") as file:
                pdf_reader = PyPDF2.PdfReader(file)
                return len(pdf_reader.pages)
        except Exception as e:
            logging.error(f"Error counting PDF pages: {e}")
            return 0

    def get_default_email_body(self):
        """Get the default email body template."""
        return """Hello!

Your EPUB to PDF conversion is complete!

Book Title: {book_title}
Total Pages: {page_count}

Please find the converted PDF attached to this email.

Best regards,
EPUB to PDF Converter"""

    def process_email_body(self, email_body, book_title, page_count):
        """Process email body by replacing placeholders with actual values."""
        if not email_body or email_body.strip() == "":
            email_body = self.get_default_email_body()
        
        # Replace placeholders with actual values
        processed_body = email_body.replace("{book_title}", str(book_title))
        processed_body = processed_body.replace("{page_count}", str(page_count))
        
        return processed_body

    def send_email_with_pdf(self, recipient_email, pdf_path, book_title, page_count, custom_email_body=None):
        """Send PDF via email with custom email body support."""
        try:
            # Email configuration
            smtp_server = "smtp.gmail.com"
            smtp_port = 587
            sender_email = "mr.umaroff@gmail.com"
            sender_password = "mhwb iwfn epsc glnt"
            
            # Create message
            msg = MIMEMultipart()
            msg["From"] = sender_email
            msg["To"] = recipient_email
            msg["Subject"] = f"Your converted PDF: {book_title}"
            
            # Process email body
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
            logging.error(f"Error sending email: {e}")
            return False

    def process_html_content(self, html_content, body_style, h1_style, h2_style, h3_style, image_map, frame_width, frame_height):
        """Process HTML content and convert to ReportLab flowables."""
        flowables = []
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Define a helper function for recursive processing
        def parse_element(element):
            if isinstance(element, NavigableString):
                text = str(element).strip()
                if text:
                    return [Paragraph(text, body_style)]
                return []

            if element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                level = int(element.name[1])
                text = element.get_text(strip=True)
                if text:
                    if level == 1:
                        return [Paragraph(text, h1_style), Spacer(1, 0.15 * inch)]
                    elif level == 2:
                        return [Paragraph(text, h2_style), Spacer(1, 0.15 * inch)]
                    else:
                        return [Paragraph(text, h3_style), Spacer(1, 0.15 * inch)]
                return []
            
            elif element.name == "p":
                # Handle paragraphs, including nested formatting
                combined_text = ""
                for content in element.contents:
                    if isinstance(content, NavigableString):
                        combined_text += html.escape(str(content))
                    elif isinstance(content, Tag):
                        if content.name in ["strong", "b"]:
                            combined_text += f"<b>{html.escape(content.get_text())}</b>"
                        elif content.name in ["em", "i"]:
                            combined_text += f"<i>{html.escape(content.get_text())}</i>"
                        elif content.name == "br":
                            combined_text += "<br/>"
                        else:
                            # Fallback for other tags within a paragraph
                            combined_text += html.escape(content.get_text())
                if combined_text.strip():
                    return [Paragraph(combined_text, body_style), Spacer(1, 0.1 * inch)]
                return []
            
            elif element.name == "img" and element.get("src"):
                img_src_base = os.path.basename(element["src"])
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
                        return [rl_image, Spacer(1, 0.2 * inch)]
                    except Exception as e:
                        logging.warning(f"Error processing image: {e}")
                return []
            
            elif element.name == "div":
                div_flowables = []
                for child in element.contents:
                    div_flowables.extend(parse_element(child))
                return div_flowables
            
            elif element.name in ["strong", "b"]:
                text = element.get_text(strip=True)
                if text:
                    return [Paragraph(f"<b>{text}</b>", body_style)]
                return []
            
            elif element.name in ["em", "i"]:
                text = element.get_text(strip=True)
                if text:
                    return [Paragraph(f"<i>{text}</i>", body_style)]
                return []
            
            elif element.name == "br":
                return [Spacer(1, 0.1 * inch)]
            
            elif element.name == "hr":
                return [Spacer(1, 0.3 * inch), Paragraph("―" * 50, body_style), Spacer(1, 0.3 * inch)]
            
            elif element.name == "blockquote":
                text = element.get_text(strip=True)
                if text:
                    return [Paragraph(f"<para leftIndent=\"20\" spaceAfter=\"10\"><i>\"{text}\"</i></para>", body_style)]
                return []
            
            elif element.name in ["ul", "ol"]:
                list_items = []
                for li in element.find_all("li", recursive=False):
                    combined_text = ""
                    for child in li.contents:
                        if isinstance(child, NavigableString):
                            combined_text += html.escape(str(child))
                        elif isinstance(child, Tag):
                            if child.name in ["strong", "b"]:
                                combined_text += f"<b>{html.escape(child.get_text())}</b>"
                            elif child.name in ["em", "i"]:
                                combined_text += f"<i>{html.escape(child.get_text())}</i>"
                            else:
                                combined_text += html.escape(child.get_text())
                    if combined_text.strip():
                        list_items.append(ListItem(Paragraph(combined_text, body_style), leftIndent=20))
                
                if list_items:
                    bullet_style = "bullet" if element.name == "ul" else "1"
                    return [ListFlowable(list_items, bulletType=bullet_style), Spacer(1, 0.2 * inch)]
                return []
            
            # Add handling for other common tags like pre, code, tables if necessary
            elif element.name == "pre":
                text = element.get_text()
                if text:
                    # For preformatted text, use a monospaced font and preserve whitespace
                    pre_style = ParagraphStyle("Code", parent=body_style, fontName="Courier", fontSize=body_style.fontSize * 0.9, leading=body_style.leading, alignment=TA_LEFT)
                    return [Paragraph(html.escape(text), pre_style), Spacer(1, 0.1 * inch)]
                return []

            elif element.name == "code":
                text = element.get_text()
                if text:
                    code_style = ParagraphStyle("InlineCode", parent=body_style, fontName="Courier", fontSize=body_style.fontSize * 0.9)
                    return [Paragraph(f"<font name=\"Courier\">{html.escape(text)}</font>", code_style)]
                return []

            # Fallback for other elements: try to extract text content
            else:
                text = element.get_text(strip=True)
                if text:
                    return [Paragraph(text, body_style)]
                return []

        # Process all top-level elements in the soup
        for element in soup.contents:
            flowables.extend(parse_element(element))
        
        logging.info(f"Generated {len(flowables)} flowables from HTML content.")
        return flowables

    def build_story(self, doc, book_title, author_name, book_description, spine_items, content_map, image_map,
                    font_size, line_spacing, has_full_page_image, frame_width, frame_height):
        """Build the PDF story from EPUB content."""
        story = []
        styles = getSampleStyleSheet()
        leading = font_size * line_spacing

        # Define styles
        body_style = ParagraphStyle("BodyText", parent=styles["Normal"], fontName="DejaVu-Sans",
                                    fontSize=font_size, leading=leading, alignment=TA_JUSTIFY)
        h1_style = ParagraphStyle("H1", parent=styles["h1"], fontName="DejaVu-Sans-Bold",
                                  fontSize=20, leading=24, spaceAfter=12, alignment=TA_CENTER)
        h2_style = ParagraphStyle("H2", parent=styles["h2"], fontName="DejaVu-Sans-Bold",
                                  fontSize=16, leading=20, spaceAfter=10, alignment=TA_LEFT)
        h3_style = ParagraphStyle("H3", parent=styles["h3"], fontName="DejaVu-Sans-Bold",
                                  fontSize=14, leading=18, spaceAfter=8, alignment=TA_LEFT)
        toc_style = ParagraphStyle("TOC", parent=styles["Normal"], fontName="DejaVu-Sans",
                                   fontSize=14, leading=18, leftIndent=inch*0.25)
        title_page_title_style = ParagraphStyle("TitlePageTitle", parent=styles["h1"], fontName="DejaVu-Sans-Bold",
                                               fontSize=30, textColor=colors.black, alignment=TA_CENTER)
        title_page_author_style = ParagraphStyle("TitlePageAuthor", parent=styles["Normal"], fontName="DejaVu-Sans-Italic",
                                                fontSize=18, textColor=colors.black, alignment=TA_CENTER, spaceBefore=12)
        description_style = ParagraphStyle("Description", parent=body_style, textColor=colors.white,
                                          backColor=colors.Color(0,0,0,0.6), alignment=TA_CENTER,
                                          borderPadding=20, borderRadius=15)

        # Cover Page (always the first page)
        story.append(NextPageTemplate("CoverPage"))
        story.append(PageBreak())

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
        
        # Set up single page template for main content
        story.append(NextPageTemplate("ContentPage"))
        # Add a dummy flowable to ensure the first content page is properly initialized
        story.append(Spacer(1, 0.01 * inch))
        story.append(PageBreak())

        # Table of contents
        toc_page_content = [Paragraph("Содержание", h1_style), Spacer(1, 0.25*inch)]
        chapter_content_story = []
        toc_links = []

        # Debugging: Print spine_items and content_map keys
        logging.info(f"Spine items: {[item for item in spine_items]}")
        logging.info(f"Content map keys: {[key for key in content_map.keys()]}")

        # Process each spine item in order
        for i, item_id in enumerate(spine_items):
            content_html = content_map.get(item_id)
            if not content_html:
                logging.warning(f"Content for item_id {item_id} not found in content_map. Skipping.")
                continue

            # Create anchor for TOC
            anchor_key = f"chapter_{i}"
            toc_links.append((item_id, anchor_key))
            
            # Add chapter title to TOC
            toc_page_content.append(Paragraph(f"<a href=\"#{anchor_key}\">{item_id}</a>", toc_style))
            
            # Process chapter content
            chapter_content_story.append(PageBreak())
            chapter_content_story.append(Paragraph(f"<a name=\"{anchor_key}\"/>{item_id}", h1_style))
            
            # Process HTML content
            chapter_flowables = self.process_html_content(
                content_html, 
                body_style, 
                h1_style, 
                h2_style, 
                h3_style,
                image_map,
                frame_width,
                frame_height
            )
            logging.info(f"Chapter {item_id} generated {len(chapter_flowables)} flowables.")
            chapter_content_story.extend(chapter_flowables)

        # Add TOC and content to story
        story.extend(toc_page_content)
        story.extend(chapter_content_story)

        logging.info(f"Total flowables in story: {len(story)}")

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
                except Exception as e:
                    logging.warning(f"Error removing temporary file {path}: {e}")
        if os.path.exists(temp_dir):
            try:
                os.rmdir(temp_dir)
            except Exception as e:
                logging.warning(f"Error removing temporary directory {temp_dir}: {e}")

    def convert_epub_to_pdf(self, epub_file_path, output_pdf_path, email_recipient=None, custom_email_body=None):
        temp_dir = None
        temp_files = []
        try:
            temp_dir = tempfile.mkdtemp()

            book = epub.read_epub(epub_file_path)
            book_title = book.get_metadata("DC", "title")[0][0] if book.get_metadata("DC", "title") else "Untitled"
            author_name = book.get_metadata("DC", "creator")[0][0] if book.get_metadata("DC", "creator") else "Unknown Author"
            book_description = book.get_metadata("DC", "description")[0][0] if book.get_metadata("DC", "description") else ""

            cover_path = None
            title_bg_path = None
            blurred_cover_path = None
            full_page_image_path = None
            has_full_page_image = False

            image_map = {}
            for item in book.get_items():
                if item.get_type() == ITEM_IMAGE:
                    image_map[os.path.basename(item.file_name)] = item.content
                    if "cover" in item.file_name.lower() or "cover" in item.id.lower():
                        cover_path = os.path.join(temp_dir, os.path.basename(item.file_name))
                        with open(cover_path, "wb") as f:
                            f.write(item.content)
                        temp_files.append(cover_path)

            # Generate blurred cover and title page background
            if cover_path and os.path.exists(cover_path):
                try:
                    with Image.open(cover_path) as img:
                        # Title page background (slightly blurred)
                        title_bg_img = img.filter(ImageFilter.GaussianBlur(radius=5))
                        title_bg_path = os.path.join(temp_dir, "title_bg.png")
                        title_bg_img.save(title_bg_path)
                        temp_files.append(title_bg_path)

                        # Blurred cover for final page (more blurred)
                        blurred_img = img.filter(ImageFilter.GaussianBlur(radius=10))
                        blurred_cover_path = os.path.join(temp_dir, "blurred_cover.png")
                        blurred_img.save(blurred_cover_path)
                        temp_files.append(blurred_cover_path)
                except Exception as e:
                    logging.warning(f"Could not process cover image for blurring: {e}")

            content_map = {}
            spine_items = []
            for item in book.spine:
                epub_item = book.get_item_with_id(item[0])
                if epub_item and epub_item.get_type() == ITEM_DOCUMENT:
                    content_map[epub_item.file_name] = epub_item.content.decode("utf-8")
                    spine_items.append(epub_item.file_name)

            # Set up document and frames
            doc = BaseDocTemplate(output_pdf_path, pagesize=letter)
            
            # Margins
            inner_margin = inch
            outer_margin = inch
            top_bottom_margin = inch

            # Calculate frame dimensions based on margins
            frame_width = letter[0] - (inner_margin + outer_margin)
            frame_height = letter[1] - (2 * top_bottom_margin)

            # Create a single frame for content pages
            content_frame = Frame(doc.leftMargin + inner_margin, doc.bottomMargin + top_bottom_margin,
                                  frame_width, frame_height, id="normalFrame")

            # PageDrawer instance
            page_drawer = PageDrawer(cover_path, title_bg_path, blurred_cover_path, full_page_image_path,
                                     book_title, author_name, inner_margin, outer_margin, top_bottom_margin)

            # Page Templates
            doc.addPageTemplates([
                PageTemplate(id="CoverPage", frames=[], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id="TitlePage", frames=[], onPage=page_drawer.title_page_background),
                PageTemplate(id="ContentPage", frames=[content_frame], onPage=page_drawer.cover_and_content_pages),
                PageTemplate(id="FullImagePage", frames=[], onPage=page_drawer.full_image_page_background),
                PageTemplate(id="FinalPage", frames=[], onPage=page_drawer.final_page_background)
            ])

            story = self.build_story(doc, book_title, author_name, book_description, spine_items, content_map, image_map,
                                     font_size=12, line_spacing=1.2, has_full_page_image=has_full_page_image,
                                     frame_width=frame_width, frame_height=frame_height)

            # Ensure story is not empty before building the document
            if not story:
                logging.error("Story is empty. No content to build PDF.")
                raise ValueError("No content to build PDF.")

            doc.build(story)

            # Send email if recipient is provided
            if email_recipient:
                page_count = self.count_pdf_pages(output_pdf_path)
                self.send_email_with_pdf(email_recipient, output_pdf_path, book_title, page_count, custom_email_body)

            return output_pdf_path

        except Exception as e:
            logging.error(f"Error converting EPUB to PDF: {e}")
            raise
        finally:
            self.cleanup_temp_files(temp_files, temp_dir)

@converter_bp.route("/convert", methods=["POST"])
def convert_epub():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    if file and file.filename.endswith(".epub"):
        try:
            # Generate a unique ID for this conversion
            conversion_id = str(uuid.uuid4())
            conversion_status[conversion_id] = {"status": "pending", "progress": 0, "file_path": None, "error": None}

            # Save the uploaded EPUB file temporarily
            temp_epub_path = os.path.join(tempfile.gettempdir(), f"{conversion_id}.epub")
            file.save(temp_epub_path)

            output_pdf_path = os.path.join(tempfile.gettempdir(), f"{conversion_id}.pdf")
            email_recipient = request.form.get("email")
            custom_email_body = request.form.get("email_body")

            # Run conversion in a separate thread
            def conversion_thread():
                try:
                    converter = EpubToPdfConverter()
                    converter.convert_epub_to_pdf(temp_epub_path, output_pdf_path, email_recipient, custom_email_body)
                    conversion_status[conversion_id]["status"] = "completed"
                    conversion_status[conversion_id]["file_path"] = output_pdf_path
                except Exception as e:
                    conversion_status[conversion_id]["status"] = "failed"
                    conversion_status[conversion_id]["error"] = str(e)
                finally:
                    # Clean up the temporary epub file
                    if os.path.exists(temp_epub_path):
                        os.remove(temp_epub_path)

            thread = threading.Thread(target=conversion_thread)
            thread.start()

            return jsonify({"message": "Conversion started", "conversion_id": conversion_id}), 202
        except Exception as e:
            logging.error(f"File upload or initial processing error: {e}")
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Invalid file type. Please upload an EPUB file."}), 400

@converter_bp.route("/status/<conversion_id>", methods=["GET"])
def get_conversion_status(conversion_id):
    status = conversion_status.get(conversion_id)
    if not status:
        return jsonify({"error": "Conversion ID not found"}), 404
    
    return jsonify(status), 200

@converter_bp.route("/download/<conversion_id>", methods=["GET"])
def download_pdf(conversion_id):
    status = conversion_status.get(conversion_id)
    if not status or status["status"] != "completed" or not status["file_path"]:
        return jsonify({"error": "Conversion not completed or file not found"}), 404
    
    pdf_path = status["file_path"]
    if not os.path.exists(pdf_path):
        return jsonify({"error": "PDF file not found on server"}), 404
    
    # Clean up after download (optional, depending on retention policy)
    # del conversion_status[conversion_id]
    # os.remove(pdf_path)

    return send_file(pdf_path, as_attachment=True, download_name=os.path.basename(pdf_path))

# Cleanup old conversion statuses periodically
def cleanup_old_statuses():
    while True:
        now = datetime.now()
        keys_to_delete = []
        for conv_id, status_data in conversion_status.items():
            # Delete statuses older than 1 hour
            if "timestamp" in status_data and (now - status_data["timestamp"]) > timedelta(hours=1):
                keys_to_delete.append(conv_id)
        
        for key in keys_to_delete:
            if conversion_status[key]["file_path"] and os.path.exists(conversion_status[key]["file_path"]):
                os.remove(conversion_status[key]["file_path"])
            del conversion_status[key]
        
        # Run every 30 minutes
        threading.Event().wait(1800)

# Start cleanup thread
# cleanup_thread = threading.Thread(target=cleanup_old_statuses)
# cleanup_thread.daemon = True
# cleanup_thread.start()
