import os
import sqlite3
import base64
import urllib.parse
import json
import re
from datetime import datetime
from io import BytesIO
import pandas as pd
import streamlit as st

from num2words import num2words
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# ---------------------------------------------------------
# 1. DATABASE MANAGEMENT (SQLite)
# ---------------------------------------------------------
DB_FILE = "omohtech_billing.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS clients (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE,
                        address TEXT,
                        city TEXT,
                        country TEXT,
                        phone TEXT
                    )''')
        c.execute('''CREATE TABLE IF NOT EXISTS inventory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_name TEXT UNIQUE,
                        default_price REAL,
                        stock_qty INTEGER DEFAULT 10
                    )''')
        
        # Ensure stock_qty column exists if upgrading from previous version
        c.execute("PRAGMA table_info(inventory)")
        columns = [col[1] for col in c.fetchall()]
        if "stock_qty" not in columns:
            c.execute("ALTER TABLE inventory ADD COLUMN stock_qty INTEGER DEFAULT 10")

        c.execute('''CREATE TABLE IF NOT EXISTS document_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        doc_num TEXT UNIQUE,
                        doc_type TEXT,
                        client_name TEXT,
                        total_amount REAL,
                        status TEXT,
                        items_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
        c.execute('''CREATE TABLE IF NOT EXISTS ref_sequences (
                        doc_type TEXT PRIMARY KEY,
                        last_seq INTEGER
                    )''')
        
        c.execute("SELECT COUNT(*) FROM inventory")
        if c.fetchone()[0] == 0:
            default_items = [
                ("DESKTOP COMPUTER CORE I3 13TH GEN", 450000.0, 5),
                ("KEYBOARD", 8500.0, 25),
                ("MOUSE (WIRELESS)", 6500.0, 30),
                ("512GB M.2 NVME SSD HIKSEMI WAVE", 80000.0, 12),
                ("1TB EXTERNAL HARD DRIVE", 65000.0, 8),
                ("CAT6 NETWORK CABLE (305M ROLL)", 120000.0, 4),
                ("24-PORT GIGABIT SWITCH TP-LINK", 135000.0, 3),
                ("MIKROTIK ROUTERBOARD RB750Gr3", 95000.0, 6),
                ("ZKTeco K40 BIOMETRIC TERMINAL", 110000.0, 2),
            ]
            c.executemany("INSERT INTO inventory (item_name, default_price, stock_qty) VALUES (?, ?, ?)", default_items)
            
        conn.commit()

def get_next_ref_num(doc_type):
    prefix = "INV" if doc_type == "Invoice" else "REC"
    year = datetime.now().year
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT last_seq FROM ref_sequences WHERE doc_type = ?", (doc_type,))
        row = c.fetchone()
        if row:
            next_seq = row[0] + 1
        else:
            next_seq = 1001
    return f"{prefix}-{year}-{next_seq}"

def commit_next_ref_num(doc_type):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT last_seq FROM ref_sequences WHERE doc_type = ?", (doc_type,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE ref_sequences SET last_seq = last_seq + 1 WHERE doc_type = ?", (doc_type,))
        else:
            c.execute("INSERT INTO ref_sequences (doc_type, last_seq) VALUES (?, ?)", (doc_type, 1001))
        conn.commit()

def save_client(name, address, city, country, phone):
    if not name.strip():
        return
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO clients (name, address, city, country, phone)
                     VALUES (?, ?, ?, ?, ?)''', (name.strip(), address, city, country, phone))
        conn.commit()

def get_clients():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT name, address, city, country, phone FROM clients ORDER BY name ASC")
        rows = c.fetchall()
    return {r[0]: {"address": r[1], "city": r[2], "country": r[3], "phone": r[4]} for r in rows}

def get_inventory():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT item_name, default_price, stock_qty FROM inventory ORDER BY item_name ASC")
        rows = c.fetchall()
    return {r[0]: {"price": r[1], "stock": r[2]} for r in rows}

def update_inventory_stock(items):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        for item in items:
            name = item.get("description", "").strip().upper()
            qty = int(item.get("quantity", 1))
            c.execute("UPDATE inventory SET stock_qty = MAX(0, stock_qty - ?) WHERE UPPER(item_name) = ?", (qty, name))
        conn.commit()

def add_or_update_item(name, price, stock):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO inventory (item_name, default_price, stock_qty)
                     VALUES (?, ?, ?)''', (name.strip().upper(), float(price), int(stock)))
        conn.commit()

def log_document(doc_num, doc_type, client_name, grand_total, status, items):
    items_json = json.dumps(items)
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO document_history 
                     (doc_num, doc_type, client_name, total_amount, status, items_json)
                     VALUES (?, ?, ?, ?, ?, ?)''', 
                  (doc_num, doc_type, client_name, grand_total, status, items_json))
        conn.commit()

def update_document_status(doc_num, new_status):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("UPDATE document_history SET status = ? WHERE doc_num = ?", (new_status, doc_num))
        conn.commit()

init_db()

# ---------------------------------------------------------
# 2. HELPER & AI FUNCTIONS
# ---------------------------------------------------------
def amount_to_words(amount):
    naira = int(amount)
    kobo = int(round((amount - naira) * 100))
    words = num2words(naira, lang='en').replace('-', ' ').title() + " Naira"
    if kobo > 0:
        words += f" and {num2words(kobo, lang='en').replace('-', ' ').title()} Kobo"
    return words + " Only"

def parse_items_with_ai(prompt_text, inventory_dict):
    extracted_items = []
    lines = prompt_text.split('\n')
    for line in lines:
        if not line.strip():
            continue
        matched_catalog = None
        for item_name in inventory_dict.keys():
            if item_name.lower() in line.lower():
                matched_catalog = item_name
                break

        qty_match = re.search(r'(\d+)\s*(pcs|units|items|x|\b)', line, re.IGNORECASE)
        price_match = re.search(r'(\d+[\d,]*\b000|\d+k|\d+)', line, re.IGNORECASE)

        qty = int(qty_match.group(1)) if qty_match else 1
        desc = matched_catalog if matched_catalog else line.strip().upper()
        
        price = 0.0
        if matched_catalog:
            price = inventory_dict[matched_catalog]["price"]
        elif price_match:
            raw_p = price_match.group(0).lower().replace(',', '')
            price = float(raw_p.replace('k', '')) * 1000 if 'k' in raw_p else float(raw_p)

        extracted_items.append({"description": desc, "quantity": qty, "price": price})

    return extracted_items if extracted_items else None

def generate_whatsapp_link(phone_number, doc_type, doc_num, client_name, grand_total, bank_info):
    clean_phone = "".join(filter(str.isdigit, str(phone_number)))
    if clean_phone.startswith("0"):
        clean_phone = "234" + clean_phone[1:]
        
    msg = (
        f"Hello *{client_name}*,\n\n"
        f"Here is your official *{doc_type}* from *OMOHTECH CONCEPTS SOLUTIONS*.\n\n"
        f"📄 *{doc_type} Reference:* {doc_num}\n"
        f"💰 *Total Amount:* ₦{grand_total:,.2f}\n\n"
        f"🏦 *Payment Details:*\n"
        f"Bank: {bank_info['bank_name']}\n"
        f"Account Number: {bank_info['acc_num']}\n"
        f"Account Name: {bank_info['acc_name']}\n\n"
        f"Thank you for doing business with us!"
    )
    return f"https://wa.me/{clean_phone}?text={urllib.parse.quote(msg)}"

# ---------------------------------------------------------
# 3. AUTO-FITTING REPORTLAB PDF ENGINE
# ---------------------------------------------------------
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_decorations(self, page_count):
        self.saveState()
        status = getattr(self, 'doc_status', 'BLANK')
        doc_type = getattr(self, 'doc_type', 'Invoice')
        primary_hex = '#DC2626' if doc_type == "Invoice" else '#003399'

        if status == 'PAID':
            self.setFont('Helvetica-Bold', 60)
            self.setFillColor(colors.HexColor('#10B981'), alpha=0.10)
            self.rotate(30)
            self.drawString(320, 200, "PAID")
            self.rotate(-30)

        self.setStrokeColor(colors.HexColor(primary_hex))
        self.setLineWidth(2)
        self.line(0.5 * inch, 0.6 * inch, 8.0 * inch, 0.6 * inch)

        self.setFont('Helvetica-Bold', 8)
        self.setFillColor(colors.HexColor(primary_hex))
        self.drawString(0.5 * inch, 0.42 * inch, "OMOHTECH CONCEPTS SOLUTIONS")
        
        self.setFont('Helvetica', 8)
        self.setFillColor(colors.HexColor('#64748B'))
        self.drawString(2.6 * inch, 0.42 * inch, "|  Official Enterprise Document")
        self.drawRightString(8.0 * inch, 0.42 * inch, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def generate_pdf(doc_type, client_info, company_info, items, account_info, 
                 tax_rate=0.0, discount_amount=0.0, status="BLANK", 
                 signature_path=None, logo_path=None):
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=54
    )
    story = []
    styles = getSampleStyleSheet()

    PRIMARY_COLOR = colors.HexColor("#DC2626") if doc_type == "Invoice" else colors.HexColor("#003399")
    DARK_SLATE = colors.HexColor("#1E293B")
    LIGHT_BG = colors.HexColor("#F8FAFC")
    BORDER_COLOR = colors.HexColor("#E2E8F0")

    style_company_title = ParagraphStyle('CompTitle', fontName='Helvetica-Bold', fontSize=16, leading=18, textColor=PRIMARY_COLOR, alignment=1)
    style_company_sub = ParagraphStyle('CompSub', fontName='Helvetica', fontSize=8.5, leading=11, textColor=colors.HexColor("#475569"), alignment=1)
    
    style_doc_title = ParagraphStyle('DocTitle', fontName='Helvetica-Bold', fontSize=22, leading=24, textColor=PRIMARY_COLOR, alignment=1)
    style_doc_ref = ParagraphStyle('DocRef', fontName='Helvetica-Bold', fontSize=9, textColor=DARK_SLATE, alignment=1)

    style_card_head = ParagraphStyle('CardHead', fontName='Helvetica-Bold', fontSize=9, leading=11, textColor=PRIMARY_COLOR)
    style_card_body = ParagraphStyle('CardBody', fontName='Helvetica', fontSize=8.5, leading=12, textColor=DARK_SLATE)

    style_th = ParagraphStyle('TH', fontName='Helvetica-Bold', fontSize=8.5, textColor=colors.white)
    style_th_r = ParagraphStyle('THR', fontName='Helvetica-Bold', fontSize=8.5, textColor=colors.white, alignment=2)
    style_td = ParagraphStyle('TD', fontName='Helvetica', fontSize=8.5, leading=11, textColor=DARK_SLATE)
    style_td_r = ParagraphStyle('TDR', fontName='Helvetica', fontSize=8.5, leading=11, textColor=DARK_SLATE, alignment=2)

    # 1. HEADER
    if logo_path and os.path.exists(logo_path):
        logo_img = Image(logo_path, width=2.2*inch, height=0.75*inch)
        logo_img.hAlign = 'CENTER'
        story.append(logo_img)
        story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(f"<b>{company_info['name']}</b>", style_company_title))
        
    story.append(Paragraph(f"{company_info['address_line1']}, {company_info['address_line2']}", style_company_sub))
    story.append(Paragraph(f"Tel: {company_info['phone']} | Email: {company_info['email']}", style_company_sub))
    story.append(Spacer(1, 6))
    
    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY_COLOR, spaceBefore=4, spaceAfter=12))

    # 2. DOCUMENT TITLE & REF
    story.append(Paragraph(doc_type.upper(), style_doc_title))
    story.append(Paragraph(f"Reference No: <b>{company_info['doc_num']}</b>", style_doc_ref))
    story.append(Spacer(1, 12))

    # 3. CLIENT & METADATA CARDS
    client_card = [
        Paragraph("BILLED TO", style_card_head),
        Spacer(1, 3),
        Paragraph(f"<b>{client_info['name']}</b>", style_card_body),
        Paragraph(client_info['address'], style_card_body),
        Paragraph(f"{client_info['city']}, {client_info['country']}", style_card_body),
        Paragraph(f"Phone: {client_info['phone']}", style_card_body) if client_info['phone'] else Spacer(1, 1)
    ]

    doc_meta_card = [
        Paragraph("DOCUMENT SUMMARY", style_card_head),
        Spacer(1, 3),
        Paragraph(f"<b>Issue Date:</b> {company_info['date']}", style_card_body),
    ]
    if doc_type == "Invoice" and company_info.get('due_date'):
        doc_meta_card.append(Paragraph(f"<b>Due Date:</b> {company_info['due_date']}", style_card_body))
    
    if status != "BLANK":
        doc_meta_card.append(Paragraph(f"<b>Status Stamp:</b> {status}", style_card_body))

    info_card_table = Table([[client_card, doc_meta_card]], colWidths=[4.2*inch, 3.2*inch])
    info_card_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
        ('BOX', (0, 0), (0, 0), 0.5, BORDER_COLOR),
        ('BOX', (1, 0), (1, 0), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(info_card_table)
    story.append(Spacer(1, 12))

    # 4. AUTO-FITTING ITEMS TABLE
    table_data = [[
        Paragraph("Item & Description", style_th),
        Paragraph("Qty", style_th_r),
        Paragraph("Unit Price (NGN)", style_th_r),
        Paragraph("Amount (NGN)", style_th_r)
    ]]

    subtotal = 0.0
    for item in items:
        qty = int(item.get('quantity', 1))
        price = float(item.get('price', 0.0))
        amount = qty * price
        subtotal += amount
        table_data.append([
            Paragraph(str(item.get('description', '')), style_td),
            Paragraph(str(qty), style_td_r),
            Paragraph(f"{price:,.2f}", style_td_r),
            Paragraph(f"{amount:,.2f}", style_td_r)
        ])

    tax_val = subtotal * (tax_rate / 100.0)
    grand_total = max(0.0, subtotal + tax_val - discount_amount)

    table_data.append(["", "", Paragraph("Subtotal:", style_td_r), Paragraph(f"₦{subtotal:,.2f}", style_td_r)])
    if discount_amount > 0:
        table_data.append(["", "", Paragraph("Discount:", style_td_r), Paragraph(f"-₦{discount_amount:,.2f}", style_td_r)])
    if tax_rate > 0:
        table_data.append(["", "", Paragraph(f"VAT ({tax_rate}%):", style_td_r), Paragraph(f"₦{tax_val:,.2f}", style_td_r)])

    amount_label = "Grand Total:" if doc_type == "Invoice" else "Total Paid:"
    style_total_lbl = ParagraphStyle('TotalLbl', fontName='Helvetica-Bold', fontSize=10, textColor=DARK_SLATE, alignment=2)
    style_total_val = ParagraphStyle('TotalVal', fontName='Helvetica-Bold', fontSize=11, textColor=PRIMARY_COLOR, alignment=2)

    table_data.append(["", "", Paragraph(f"<b>{amount_label}</b>", style_total_lbl), Paragraph(f"<b>₦{grand_total:,.2f}</b>", style_total_val)])

    items_table = Table(table_data, colWidths=[4.1*inch, 0.7*inch, 1.3*inch, 1.3*inch], repeatRows=1)
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_COLOR),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LINEBELOW', (0, 1), (-1, len(items)), 0.5, BORDER_COLOR),
        ('LINEABOVE', (2, -1), (3, -1), 1.5, PRIMARY_COLOR),
        ('BACKGROUND', (2, -1), (3, -1), LIGHT_BG),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # 5. AMOUNT IN WORDS
    words_text = amount_to_words(grand_total)
    words_para = Paragraph(f"<b>Amount in Words:</b> {words_text}", ParagraphStyle('Words', fontName='Helvetica', fontSize=8.5, textColor=DARK_SLATE))
    words_table = Table([[words_para]], colWidths=[7.4*inch])
    words_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
    ]))
    story.append(words_table)
    story.append(Spacer(1, 12))

    # 6. PAYMENT INFO & SIGNATURE
    bank_content = [
        Paragraph("PAYMENT INFORMATION", style_card_head),
        Spacer(1, 3),
        Paragraph(f"<b>Bank Name:</b> {account_info['bank_name']}", style_card_body),
        Paragraph(f"<b>Account Number:</b> {account_info['acc_num']}", style_card_body),
        Paragraph(f"<b>Account Name:</b> {account_info['acc_name']}", style_card_body),
    ]

    if doc_type == "Receipt":
        bank_box = Table([[bank_content]], colWidths=[4.8*inch])
        bank_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ]))

        sig_elements = []
        if signature_path and os.path.exists(signature_path):
            sig_elements.append(Image(signature_path, width=1.4*inch, height=0.4*inch))
        else:
            sig_elements.append(Paragraph("<i>Authorized Signature</i>", ParagraphStyle('SigText', fontName='Helvetica-Oblique', fontSize=8.5, textColor=colors.HexColor("#64748B"), alignment=1)))

        sig_elements.append(HRFlowable(width="100%", thickness=0.75, color=BORDER_COLOR, spaceBefore=4, spaceAfter=2))
        sig_elements.append(Paragraph("<b>Authorized Signatory</b>", ParagraphStyle('SigLbl', fontName='Helvetica', fontSize=7.5, textColor=colors.HexColor("#64748B"), alignment=1)))

        sig_box = Table([[sig_elements]], colWidths=[2.3*inch])
        sig_box.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'BOTTOM')]))

        footer_table = Table([[bank_box, sig_box]], colWidths=[5.0*inch, 2.4*inch])
        footer_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'BOTTOM')]))
        story.append(footer_table)
    else:
        bank_box = Table([[bank_content]], colWidths=[7.4*inch])
        bank_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), LIGHT_BG),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ]))
        story.append(bank_box)

    def make_canvas(*args, **kwargs):
        c = NumberedCanvas(*args, **kwargs)
        c.doc_status = status
        c.doc_type = doc_type
        return c

    doc.build(story, canvasmaker=make_canvas)
    buffer.seek(0)
    return buffer.getvalue(), grand_total

# ---------------------------------------------------------
# 4. STREAMLIT FRONTEND
# ---------------------------------------------------------
st.set_page_config(page_title="Omohtech Concepts Solutions - Billing Portal", page_icon="💼", layout="wide")

st.markdown("""
    <style>
    .main .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    h1, h2, h3 { font-family: 'Inter', sans-serif; }
    .stButton>button { border-radius: 6px; font-weight: 600; min-height: 44px; }
    </style>
""", unsafe_allow_html=True)

st.title("💼 Omohtech Concepts Solutions — Billing Portal")

inventory_dict = get_inventory()
inventory_list = list(inventory_dict.keys())

# Low Stock Warning Banner
low_stock_items = [name for name, info in inventory_dict.items() if info["stock"] <= 3]
if low_stock_items:
    st.warning(f"⚠️ **Low Stock Alert:** The following items have 3 or fewer units left: {', '.join(low_stock_items)}")

# Sidebar Configuration
st.sidebar.header("⚙ Document Settings")
doc_type_index = 1 if st.session_state.get('set_doc_type') == "Receipt" else 0
doc_type = st.sidebar.selectbox("Document Type", ["Invoice", "Receipt"], index=doc_type_index)

if 'last_selected_type' not in st.session_state or st.session_state.last_selected_type != doc_type:
    st.session_state.last_selected_type = doc_type
    st.session_state.doc_num_val = get_next_ref_num(doc_type)

doc_num = st.sidebar.text_input("Document Reference #", value=st.session_state.doc_num_val)

status_options = ["BLANK", "PAID"]
default_status_idx = 1 if st.session_state.get('set_status') == "PAID" else 0
doc_status = st.sidebar.selectbox("Status Stamp", status_options, index=default_status_idx)

doc_date = st.sidebar.date_input("Document Date", datetime.today()).strftime("%B %d, %Y")
due_date = st.sidebar.date_input("Payment Due Date", datetime.today()).strftime("%B %d, %Y")

st.sidebar.markdown("---")
st.sidebar.header("📊 Tax & Discounts")
enable_vat = st.sidebar.checkbox("Apply 7.5% VAT", value=False)
tax_rate = 7.5 if enable_vat else 0.0
discount_amount = st.sidebar.number_input("Flat Discount (NGN)", min_value=0.0, value=0.0, step=500.0)

# AI Billing Assistant Panel
st.sidebar.markdown("---")
with st.sidebar.expander("🤖 AI Billing Assistant", expanded=False):
    st.caption("Paste quick text to auto-populate items!")
    ai_prompt = st.text_area("Prompt AI Assistant", placeholder="e.g., 2pcs 512GB M.2 NVME SSD and 1 Cat6 Cable")
    if st.button("🪄 Auto-Fill Items with AI", use_container_width=True):
        parsed = parse_items_with_ai(ai_prompt, inventory_dict)
        if parsed:
            st.session_state['line_items'] = pd.DataFrame(parsed)
            st.success("Items extracted & populated!")
            st.rerun()
        else:
            st.warning("Could not parse items. Try again.")

# Inventory Manager Panel
st.sidebar.markdown("---")
with st.sidebar.expander("📦 Inventory & Stock Manager", expanded=False):
    st.caption("Manage catalog pricing & stock levels")
    inv_name = st.text_input("Item Name", placeholder="e.g., Core i7 Laptop")
    inv_price = st.number_input("Default Price (NGN)", min_value=0.0, step=1000.0)
    inv_stock = st.number_input("Stock Quantity", min_value=0, value=10, step=1)
    if st.button("💾 Save/Update Catalog Item", use_container_width=True):
        if inv_name.strip():
            add_or_update_item(inv_name, inv_price, inv_stock)
            st.success(f"Updated {inv_name} in inventory!")
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🏢 Company Information")
company_name = st.sidebar.text_input("Company Name", "OMOHTECH CONCEPTS SOLUTIONS")
company_addr1 = st.sidebar.text_input("Address Line 1", "No 12, Tech Innovation Hub, Ikeja")
company_addr2 = st.sidebar.text_input("Address Line 2", "Lagos State, Nigeria")
company_phone = st.sidebar.text_input("Phone Number", "+234 703 435 8624")
company_email = st.sidebar.text_input("Email", "omohtechconceptsoultion@gmail.com")

st.sidebar.markdown("---")
st.sidebar.header("🏦 Payment Account Details")
bank_name = st.sidebar.text_input("Bank Name", "MONIEPOINT")
acc_num = st.sidebar.text_input("Account Number", "5342488434")
acc_name = st.sidebar.text_input("Account Name", "OMOHTECH CONCEPTS SOLUTIONS LTD")

tab1, tab2 = st.tabs(["📄 Document Generator", "📊 History & Analytics"])

with tab1:
    col_input, col_preview = st.columns([1, 1.1])

    saved_clients = get_clients()

    with col_input:
        st.subheader("1. Billed To (Client Details)")
        
        preselected_client = st.session_state.get('set_client_name', "-- Select New / Custom --")
        client_options = ["-- Select New / Custom --"] + list(saved_clients.keys())
        default_client_idx = client_options.index(preselected_client) if preselected_client in client_options else 0
        
        selected_client = st.selectbox("Quick-Load Saved Client", client_options, index=default_client_idx)
        
        if selected_client != "-- Select New / Custom --":
            c_data = saved_clients[selected_client]
            client_name = st.text_input("Client / Business Name", value=selected_client)
            client_address = st.text_input("Client Address", value=c_data["address"])
            client_city = st.text_input("City / State", value=c_data["city"])
            client_country = st.text_input("Country", value=c_data["country"])
            client_phone = st.text_input("Client Phone", value=c_data["phone"])
        else:
            client_name = st.text_input("Client / Business Name", value=st.session_state.get('set_client_name', "Jide Taiwo & Co."))
            client_address = st.text_input("Client Address", "Lagos Island")
            client_city = st.text_input("City / State", "Lagos")
            client_country = st.text_input("Country", "Nigeria")
            client_phone = st.text_input("Client Phone", "")

        save_flag = st.checkbox("💾 Save/Update Client Record in Database", value=True)

        st.markdown("---")
        st.subheader("2. Line Items (Pick, Edit & Add)")

        col_pick, col_qty = st.columns([3, 1])
        selected_catalog_item = col_pick.selectbox("🎯 Pick Item from Catalog to Edit", ["-- Custom / Free Text --"] + inventory_list)
        
        default_desc = selected_catalog_item if selected_catalog_item != "-- Custom / Free Text --" else ""
        default_price = inventory_dict.get(selected_catalog_item, {}).get("price", 0.0)
        avail_stock = inventory_dict.get(selected_catalog_item, {}).get("stock", "N/A")

        if selected_catalog_item != "-- Custom / Free Text --":
            st.caption(f"📦 **Available Stock in Database:** {avail_stock} units")

        col_desc_edit, col_price_edit, col_qty_edit = st.columns([2.5, 1.5, 1])
        item_desc_input = col_desc_edit.text_input("Editable Description", value=default_desc, placeholder="e.g. Core i5 Desktop")
        item_price_input = col_price_edit.number_input("Unit Price (NGN)", min_value=0.0, value=float(default_price), step=1000.0)
        item_qty_input = col_qty_edit.number_input("Qty", min_value=1, value=1)

        if 'line_items' not in st.session_state:
            st.session_state['line_items'] = pd.DataFrame([
                {"description": "DESKTOP COMPUTER CORE I3 13TH GEN", "quantity": 1, "price": 450000.0},
                {"description": "KEYBOARD", "quantity": 7, "price": 8500.0}
            ])

        if st.button("➕ Add Item to Table", use_container_width=True):
            if item_desc_input.strip():
                new_row = pd.DataFrame([{
                    "description": item_desc_input.strip().upper(),
                    "quantity": int(item_qty_input),
                    "price": float(item_price_input)
                }])
                st.session_state['line_items'] = pd.concat([st.session_state['line_items'], new_row], ignore_index=True)
                st.rerun()

        # Editable Data Table
        edited_df = st.data_editor(
            st.session_state['line_items'],
            num_rows="dynamic",
            column_config={
                "description": st.column_config.TextColumn("Item Description", required=True, width="large"),
                "quantity": st.column_config.NumberColumn("Qty", min_value=1, default=1, step=1, required=True),
                "price": st.column_config.NumberColumn("Unit Price (NGN)", min_value=0.0, default=0.0, format="₦%.2f", required=True),
            },
            use_container_width=True
        )

        st.session_state['line_items'] = edited_df
        items = edited_df.to_dict(orient="records")

        current_subtotal = sum(float(i.get('quantity', 1)) * float(i.get('price', 0.0)) for i in items)
        st.caption(f"💰 **Live Subtotal:** ₦{current_subtotal:,.2f}")

        st.markdown("---")
        generate_btn = st.button("⚡ Process & Generate Document", type="primary", use_container_width=True)

    if generate_btn:
        company_info = {
            "name": company_name, "address_line1": company_addr1, "address_line2": company_addr2,
            "phone": company_phone, "email": company_email,
            "doc_num": doc_num, "date": doc_date, "due_date": due_date
        }
        client_info = {
            "name": client_name, "address": client_address,
            "city": client_city, "country": client_country, "phone": client_phone
        }
        account_info = {
            "bank_name": bank_name, "acc_num": acc_num, "acc_name": acc_name
        }

        logo_path = "omohtech logo.png" if os.path.exists("omohtech logo.png") else None
        sig_path = "signature.png" if os.path.exists("signature.png") else None

        pdf_bytes, grand_total = generate_pdf(
            doc_type=doc_type,
            client_info=client_info,
            company_info=company_info,
            items=items,
            account_info=account_info,
            tax_rate=tax_rate,
            discount_amount=discount_amount,
            status=doc_status,
            signature_path=sig_path,
            logo_path=logo_path
        )

        commit_next_ref_num(doc_type)
        update_inventory_stock(items)

        st.session_state['active_pdf'] = pdf_bytes
        st.session_state['active_doc_num'] = doc_num
        st.session_state['active_doc_type'] = doc_type
        st.session_state['active_client_name'] = client_name
        st.session_state['active_grand_total'] = grand_total
        st.session_state['active_phone'] = client_phone
        st.session_state['active_account_info'] = account_info

        if save_flag:
            save_client(client_name, client_address, client_city, client_country, client_phone)
        log_document(doc_num, doc_type, client_name, grand_total, doc_status, items)
        
        # Clear pre-filled state flags
        st.session_state.pop('set_doc_type', None)
        st.session_state.pop('set_status', None)
        st.session_state.pop('set_client_name', None)

        st.toast(f"{doc_type} {doc_num} generated & stock updated!")

    with col_preview:
        st.subheader("3. Document Preview & Actions")

        if 'active_pdf' in st.session_state:
            btn_col1, btn_col2 = st.columns([1, 1])
            
            with btn_col1:
                st.download_button(
                    label=f"📥 Save & Download {st.session_state['active_doc_type']} PDF",
                    data=st.session_state['active_pdf'],
                    file_name=f"{st.session_state['active_doc_type']}_{st.session_state['active_doc_num']}.pdf",
                    mime="application/pdf",
                    type="primary",
                    use_container_width=True
                )

            with btn_col2:
                if st.session_state.get('active_phone'):
                    wa_url = generate_whatsapp_link(
                        st.session_state['active_phone'],
                        st.session_state['active_doc_type'],
                        st.session_state['active_doc_num'],
                        st.session_state['active_client_name'],
                        st.session_state['active_grand_total'],
                        st.session_state['active_account_info']
                    )
                    st.link_button("📲 Share via WhatsApp", wa_url, use_container_width=True)

            st.markdown("---")
            base64_pdf = base64.b64encode(st.session_state['active_pdf']).decode('utf-8')
            pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="680" type="application/pdf" style="border: 1px solid #E2E8F0; border-radius: 8px;"></iframe>'
            st.markdown(pdf_display, unsafe_allow_html=True)
        else:
            st.info("Fill in details and click **Process & Generate Document** to render preview.")

with tab2:
    st.subheader("📈 Financial Overview & Document Log")
    
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT doc_num, doc_type, client_name, total_amount, status, created_at, items_json FROM document_history ORDER BY id DESC")
        raw_rows = c.fetchall()

    if raw_rows:
        df = pd.DataFrame(raw_rows, columns=['Doc #', 'Type', 'Client', 'Amount (NGN)', 'Status', 'Date Generated', 'items_json'])

        f_col1, f_col2 = st.columns([2, 1])
        search_query = f_col1.text_input("🔍 Search Client or Document Ref #")
        status_filter = f_col2.multiselect("Filter Status", options=["PAID", "BLANK"], default=["PAID", "BLANK"])

        filtered_df = df.copy()
        if search_query:
            filtered_df = filtered_df[
                filtered_df['Client'].str.contains(search_query, case=False, na=False) |
                filtered_df['Doc #'].str.contains(search_query, case=False, na=False)
            ]
        if status_filter:
            filtered_df = filtered_df[filtered_df['Status'].isin(status_filter)]

        m1, m2 = st.columns(2)
        paid_sum = filtered_df[filtered_df['Status'] == 'PAID']['Amount (NGN)'].sum()
        
        m1.metric("Total Revenue Logged (PAID)", f"₦{paid_sum:,.2f}")
        m2.metric("Total Documents Filtered", len(filtered_df))

        # Render list with 1-Click Convert buttons for Invoices
        st.markdown("---")
        for idx, row in filtered_df.iterrows():
            c1, c2, c3, c4, c5 = st.columns([1.5, 1.2, 2.2, 1.5, 1.8])
            c1.write(f"**{row['Doc #']}**")
            c2.write(f"`{row['Type']}`")
            c3.write(row['Client'])
            c4.write(f"₦{row['Amount (NGN)']:,.2f}")
            
            if row['Type'] == "Invoice":
                if c5.button("🔄 Convert to Receipt", key=f"convert_{row['Doc #']}"):
                    try:
                        items_data = json.loads(row['items_json'])
                        st.session_state['line_items'] = pd.DataFrame(items_data)
                    except Exception:
                        pass
                    
                    update_document_status(row['Doc #'], "PAID")
                    
                    st.session_state['set_doc_type'] = "Receipt"
                    st.session_state['set_status'] = "PAID"
                    st.session_state['set_client_name'] = row['Client']
                    
                    st.success(f"Invoice {row['Doc #']} converted! Ready as Receipt in Generator tab.")
                    st.rerun()
            else:
                c5.write(f"✅ {row['Status']}")

    else:
        st.info("No documents generated or logged yet.")