"""
Omohtech Billing Portal
Invoices and receipts as PDFs, with a client book, a stock catalogue and a document history.

Run:  streamlit run app.py
Optional: set APP_PASSWORD (environment variable or .streamlit/secrets.toml) to require a login.
Optional: set OMOHTECH_DATA_DIR to keep the database/settings somewhere other than next to this file.
"""
from __future__ import annotations

import difflib
import hmac
import html
import json
import os
import re
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pandas as pd
import pypdfium2 as pdfium
import streamlit as st
from num2words import num2words
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable, Image as RLImage, KeepTogether, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.fonts import addMapping

# =========================================================
# 1. PATHS, SETTINGS
# =========================================================
DATA_DIR = Path(os.environ.get("OMOHTECH_DATA_DIR", Path(__file__).resolve().parent))
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "omohtech_billing.db"
LOGO_FILE = next((f for f in (DATA_DIR / "omohtech logo.png", DATA_DIR / "omohtech_logo.png") if f.exists()),
                 DATA_DIR / "omohtech logo.png")
SIGNATURE_FILE = DATA_DIR / "signature.png"
FONT_DIR = DATA_DIR / "fonts"

DEFAULT_SETTINGS = {
    "company_name": "OMOHTECH CONCEPTS SOLUTIONS",
    "company_addr1": "Tech Innovation Hub",
    "company_addr2": "Ikeja, Lagos State",
    "phones": ["07046786323"],
    "email": "omohtechconceptsoultion@gmail.com",
    "bank_name": "MONIEPOINT",
    "acc_num": "5342488434",
    "acc_name": "OMOHTECH CONCEPTS SOLUTIONS LTD",
    "vat_rate": 7.5,
    "due_days": 14,
    "low_stock_at": 3,
    "advance_pct": 70.0,
    "default_note": "Thank you for doing business with us.",
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    saved = {}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    data.update(saved)

    # Older versions stored two fixed phone fields; turn them into the new list.
    if "phones" not in saved and ("phone_primary" in saved or "phone_secondary" in saved):
        old_phones = [saved.get("phone_primary", ""), saved.get("phone_secondary", "")]
        old_phones = [x.strip() for x in old_phones if x and x.strip()]
        # the old built-in number was never a real choice, so replace it with the new default
        data["phones"] = [] if old_phones == ["+234 703 435 8624"] else old_phones
        if not data["phones"]:
            data["phones"] = list(DEFAULT_SETTINGS["phones"])
    # Same for the old built-in address that was never edited.
    if saved.get("company_addr1") == "Ikeja, Lagos State" and saved.get("company_addr2") == "Nigeria":
        data["company_addr1"] = DEFAULT_SETTINGS["company_addr1"]
        data["company_addr2"] = DEFAULT_SETTINGS["company_addr2"]
    data["phones"] = [str(x).strip() for x in data.get("phones", []) if str(x).strip()]
    return data


def save_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# =========================================================
# 2. DATABASE
# =========================================================
@contextmanager
def db():
    """One short-lived connection per operation. Commits on success, always closes."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
            address TEXT, city TEXT, country TEXT, phone TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT UNIQUE,
            default_price REAL, stock_qty INTEGER DEFAULT 10)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS document_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, doc_num TEXT UNIQUE, doc_type TEXT,
            client_name TEXT, total_amount REAL, status TEXT, items_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS item_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, price REAL DEFAULT 0)""")
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS ref_sequences (
            doc_type TEXT PRIMARY KEY, last_seq INTEGER)""")

        # Lightweight migrations for databases created by the older version
        inv_cols = [r["name"] for r in conn.execute("PRAGMA table_info(inventory)")]
        if "stock_qty" not in inv_cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN stock_qty INTEGER DEFAULT 10")
        hist_cols = [r["name"] for r in conn.execute("PRAGMA table_info(document_history)")]
        if "meta_json" not in hist_cols:
            conn.execute("ALTER TABLE document_history ADD COLUMN meta_json TEXT")

        if conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO inventory (item_name, default_price, stock_qty) VALUES (?, ?, ?)",
                [
                    ("DESKTOP COMPUTER CORE I3 13TH GEN", 450000.0, 5),
                    ("KEYBOARD", 8500.0, 25),
                    ("MOUSE (WIRELESS)", 6500.0, 30),
                    ("512GB M.2 NVME SSD HIKSEMI WAVE", 80000.0, 12),
                    ("1TB EXTERNAL HARD DRIVE", 65000.0, 8),
                    ("CAT6 NETWORK CABLE (305M ROLL)", 120000.0, 4),
                    ("24-PORT GIGABIT SWITCH TP-LINK", 135000.0, 3),
                    ("MIKROTIK ROUTERBOARD RB750Gr3", 95000.0, 6),
                    ("ZKTeco K40 BIOMETRIC TERMINAL", 110000.0, 2),
                ],
            )


def default_library() -> list[str]:
    """Common descriptions offered while typing. Editable under Catalogue > Suggestions."""
    names: list[str] = []
    configs = ["", " 8GB RAM 256GB SSD", " 8GB RAM 512GB SSD", " 16GB RAM 512GB SSD"]
    for kind in ("DESKTOP COMPUTER CORE", "LAPTOP CORE"):
        for cpu in ("I3", "I5", "I7"):
            for gen in ("10TH", "11TH", "12TH", "13TH", "14TH"):
                for cfg in configs:
                    names.append(f"{kind} {cpu} {gen} GEN{cfg}")
    names += [
        "KEYBOARD", "MOUSE (USB)", "MOUSE (WIRELESS)", "WIRELESS KEYBOARD AND MOUSE COMBO",
        "19 INCH LED MONITOR", "22 INCH LED MONITOR", "24 INCH LED MONITOR", "27 INCH LED MONITOR",
        "256GB SSD", "512GB M.2 NVME SSD", "1TB SSD", "1TB EXTERNAL HARD DRIVE", "2TB EXTERNAL HARD DRIVE",
        "16GB FLASH DRIVE", "32GB FLASH DRIVE", "8GB DDR4 RAM", "16GB DDR4 RAM", "8GB DDR5 RAM",
        "CAT6 NETWORK CABLE (305M ROLL)", "CAT6 PATCH CORD 1M", "CAT6 PATCH CORD 3M",
        "RJ45 CONNECTORS (PACK OF 100)", "24-PORT CAT6 PATCH PANEL",
        "8-PORT GIGABIT SWITCH", "16-PORT GIGABIT SWITCH", "24-PORT GIGABIT SWITCH", "48-PORT GIGABIT SWITCH",
        "POE SWITCH 8-PORT", "POE SWITCH 24-PORT", "WALL-MOUNT NETWORK CABINET 6U", "WALL-MOUNT NETWORK CABINET 9U",
        "WIRELESS ACCESS POINT", "WIFI ROUTER", "MIKROTIK ROUTERBOARD",
        "UPS 650VA", "UPS 1KVA", "UPS 2KVA", "SURGE PROTECTOR EXTENSION",
        "2MP IP CAMERA", "4MP IP CAMERA", "DOME CCTV CAMERA", "4-CHANNEL NVR", "8-CHANNEL NVR", "16-CHANNEL NVR",
        "ZKTECO BIOMETRIC TERMINAL", "LASERJET PRINTER", "TONER CARTRIDGE",
        "MICROSOFT 365 BUSINESS LICENCE (PER USER, YEARLY)", "WINDOWS 11 PRO LICENCE", "ANTIVIRUS LICENCE (1 YEAR)",
        "NETWORK INSTALLATION AND CONFIGURATION", "STRUCTURED CABLING (PER POINT)", "CCTV INSTALLATION",
        "SERVER SETUP AND CONFIGURATION", "IT SUPPORT RETAINER (MONTHLY)", "SYSTEM MAINTENANCE AND SUPPORT",
        "WEBSITE DESIGN AND HOSTING", "SOFTWARE DEVELOPMENT", "IT CONSULTANCY", "LABOUR AND INSTALLATION CHARGES",
        "TRANSPORTATION AND LOGISTICS",
    ]
    return names


def seed_library() -> None:
    with db() as conn:
        if conn.execute("SELECT 1 FROM app_meta WHERE key='library_seeded'").fetchone():
            return
        conn.executemany("INSERT OR IGNORE INTO item_library (name, price) VALUES (?, 0)",
                         [(n,) for n in default_library()])
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('library_seeded', '1')")


def get_library() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT name, price FROM item_library ORDER BY name").fetchall()
    return {r["name"]: float(r["price"] or 0.0) for r in rows}


def replace_library(df: pd.DataFrame) -> int:
    rows = []
    for r in df.to_dict("records"):
        name = str(r.get("name") or "").strip().upper()
        if not name or name == "NAN":
            continue
        price = 0.0 if pd.isna(r.get("price")) else float(r["price"])
        rows.append((name, max(price, 0.0)))
    with db() as conn:
        conn.execute("DELETE FROM item_library")
        conn.executemany("INSERT OR REPLACE INTO item_library (name, price) VALUES (?, ?)", rows)
    return len(rows)


def doc_exists(doc_num: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM document_history WHERE doc_num = ?", (doc_num,)).fetchone() is not None


def get_next_ref_num(doc_type: str) -> str:
    prefix = "INV" if doc_type == "Invoice" else "REC"
    year = datetime.now().year
    with db() as conn:
        row = conn.execute("SELECT last_seq FROM ref_sequences WHERE doc_type = ?", (doc_type,)).fetchone()
    seq = (row["last_seq"] + 1) if row else 1001
    while doc_exists(f"{prefix}-{year}-{seq}"):
        seq += 1
    return f"{prefix}-{year}-{seq}"


def _bump_sequence(conn, doc_type: str, doc_num: str) -> None:
    """Move the counter up to whatever number was just used (also covers manually typed references)."""
    m = re.search(r"(\d+)$", doc_num)
    used = int(m.group(1)) if m else 0
    row = conn.execute("SELECT last_seq FROM ref_sequences WHERE doc_type = ?", (doc_type,)).fetchone()
    if row:
        if used > row["last_seq"]:
            conn.execute("UPDATE ref_sequences SET last_seq = ? WHERE doc_type = ?", (used, doc_type))
    else:
        conn.execute("INSERT INTO ref_sequences (doc_type, last_seq) VALUES (?, ?)", (doc_type, max(used, 1001)))


def get_clients() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT name, address, city, country, phone FROM clients ORDER BY name").fetchall()
    return {r["name"]: {"address": r["address"] or "", "city": r["city"] or "",
                        "country": r["country"] or "", "phone": r["phone"] or ""} for r in rows}


def get_inventory() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT item_name, default_price, stock_qty FROM inventory ORDER BY item_name").fetchall()
    return {r["item_name"]: {"price": r["default_price"] or 0.0, "stock": r["stock_qty"] or 0} for r in rows}


def get_suggestions() -> dict:
    """Everything offered while typing an item: the catalogue, items used on earlier documents
    (with the price last charged), then the editable suggestion library. {NAME: {"price", "stock"}}"""
    out = {k.upper(): dict(v) for k, v in get_inventory().items()}
    with db() as conn:
        rows = conn.execute("SELECT items_json FROM document_history ORDER BY id DESC LIMIT 400").fetchall()
    for r in rows:
        for it in safe_json(r["items_json"], []) or []:
            name = str(it.get("description", "")).strip().upper()
            if name and name not in out:
                out[name] = {"price": float(it.get("price") or 0.0), "stock": None}
    for name, price in get_library().items():
        out.setdefault(name.upper(), {"price": price, "stock": None})
    return dict(sorted(out.items()))


def replace_inventory(df: pd.DataFrame) -> int:
    """Replace the whole catalogue with the edited table (supports add, edit and delete)."""
    rows = []
    for r in df.to_dict("records"):
        name = str(r.get("item_name") or "").strip().upper()
        if not name or name == "NAN":
            continue
        price = 0.0 if pd.isna(r.get("default_price")) else float(r["default_price"])
        stock = 0 if pd.isna(r.get("stock_qty")) else int(r["stock_qty"])
        rows.append((name, price, max(stock, 0)))
    with db() as conn:
        conn.execute("DELETE FROM inventory")
        conn.executemany(
            "INSERT OR REPLACE INTO inventory (item_name, default_price, stock_qty) VALUES (?, ?, ?)", rows)
    return len(rows)


def _adjust_stock(conn, items: list[dict], sign: int) -> None:
    for it in items:
        name = str(it.get("description", "")).strip().upper()
        qty = int(it.get("quantity", 1))
        if sign < 0:
            conn.execute("UPDATE inventory SET stock_qty = MAX(0, stock_qty - ?) WHERE UPPER(item_name) = ?", (qty, name))
        else:
            conn.execute("UPDATE inventory SET stock_qty = stock_qty + ? WHERE UPPER(item_name) = ?", (qty, name))


def save_document(doc_type, doc_num, client, items, total, status, meta, deduct_stock, remember_client):
    """Everything about saving a document happens in one transaction."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM document_history WHERE doc_num = ?", (doc_num,)).fetchone():
            raise ValueError(f"{doc_num} already exists. Use a different reference number.")
        conn.execute(
            """INSERT INTO document_history
               (doc_num, doc_type, client_name, total_amount, status, items_json, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc_num, doc_type, client["name"], total, status, json.dumps(items), json.dumps(meta)),
        )
        _bump_sequence(conn, doc_type, doc_num)
        if deduct_stock:
            _adjust_stock(conn, items, -1)
        if remember_client and client["name"].strip():
            conn.execute(
                """INSERT INTO clients (name, address, city, country, phone) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET address=excluded.address, city=excluded.city,
                   country=excluded.country, phone=excluded.phone""",
                (client["name"].strip(), client["address"], client["city"], client["country"], client["phone"]),
            )
        src = meta.get("source_invoice")
        if src:
            conn.execute("UPDATE document_history SET status = 'PAID' WHERE doc_num = ?", (src,))


def set_document_status(doc_num: str, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE document_history SET status = ? WHERE doc_num = ?", (status, doc_num))


def delete_document(doc_id: int, restock: bool) -> None:
    with db() as conn:
        row = conn.execute("SELECT items_json FROM document_history WHERE id = ?", (doc_id,)).fetchone()
        if row and restock:
            try:
                _adjust_stock(conn, safe_json(row["items_json"], []), +1)
            except ValueError:
                pass
        conn.execute("DELETE FROM document_history WHERE id = ?", (doc_id,))


def safe_json(value, default):
    """json.loads that never crashes. Old rows hold NULL, which pandas turns into NaN (a float)."""
    if not isinstance(value, (str, bytes, bytearray)) or not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def get_history() -> pd.DataFrame:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, doc_num, doc_type, client_name, total_amount, status,
                      created_at, items_json, meta_json
               FROM document_history ORDER BY id DESC""").fetchall()
    df = pd.DataFrame([dict(r) for r in rows], columns=[
        "id", "doc_num", "doc_type", "client_name", "total_amount", "status",
        "created_at", "items_json", "meta_json"])
    if df.empty:
        return df

    def meta_of(s):
        m = safe_json(s, {})
        return m if isinstance(m, dict) else {}

    df["meta"] = df["meta_json"].map(meta_of)
    df["source_invoice"] = df["meta"].map(lambda m: m.get("source_invoice"))
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df


def revenue_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Paid documents, counting each sale once (a receipt created from an invoice is not new money)."""
    if df.empty:
        return df
    paid = df[df["status"] == "PAID"]
    return paid[~((paid["doc_type"] == "Receipt") & paid["source_invoice"].notna())]


init_db()
seed_library()


# =========================================================
# 3. BUSINESS LOGIC
# =========================================================
def compute_totals(items: list[dict], vat_rate: float, discount: float) -> dict:
    subtotal = round(sum(i["quantity"] * i["price"] for i in items), 2)
    discount = round(min(max(discount, 0.0), subtotal), 2)
    taxable = subtotal - discount                      # VAT is charged on the discounted amount
    vat = round(taxable * vat_rate / 100.0, 2)
    return {"subtotal": subtotal, "discount": discount, "vat": vat,
            "vat_rate": vat_rate, "total": round(taxable + vat, 2)}


def amount_to_words(amount: float) -> str:
    total_kobo = int(round(amount * 100))
    naira, kobo = divmod(total_kobo, 100)
    words = num2words(naira, lang="en").replace("-", " ").title() + " Naira"
    if kobo:
        words += f" and {num2words(kobo, lang='en').replace('-', ' ').title()} Kobo"
    return words + " Only"


def clean_items(df: pd.DataFrame | None) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    out = []
    for r in df.to_dict("records"):
        desc = "" if pd.isna(r.get("description")) else str(r["description"]).strip()
        if not desc:
            continue
        try:
            qty = int(float(r.get("quantity")))
        except (TypeError, ValueError):
            qty = 1
        price = r.get("price")
        price = 0.0 if price is None or pd.isna(price) else float(price)
        if qty > 0:
            out.append({"description": desc.upper(), "quantity": qty, "price": round(price, 2)})
    return out


def items_to_df(items: list[dict]) -> pd.DataFrame:
    if not items:
        return pd.DataFrame({"description": pd.Series(dtype="str"),
                             "quantity": pd.Series(dtype="int"),
                             "price": pd.Series(dtype="float")})
    return pd.DataFrame(items)[["description", "quantity", "price"]]


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\.]+", s.lower()))


def parse_quick_items(text: str, inventory: dict) -> list[dict]:
    """Turn lines like '2pcs 512GB nvme ssd', 'Cat6 cable x3' or 'router @ 95k' into line items."""
    rows = []
    for raw in re.split(r"[\n;]+", text or ""):
        line = raw.strip(" \t-•*,")
        if not line:
            continue
        qty, price = 1, None

        pm = re.search(r"(?:@|\bat\b|\bfor\b)\s*₦?\s*([\d,]*\.?\d+)\s*(k)?\b", line, re.I)
        if pm:
            price = float(pm.group(1).replace(",", "")) * (1000 if pm.group(2) else 1)
            line = (line[:pm.start()] + line[pm.end():]).strip()

        m = re.match(r"^(\d+)\s*(?:x|pcs?|units?|nos?)?\s+(?:of\s+)?(.+)$", line, re.I)
        if m:
            qty, line = int(m.group(1)), m.group(2)
        else:
            m = re.match(r"^(.+?)\s*(?:x|qty:?)\s*(\d+)$", line, re.I)
            if m:
                line, qty = m.group(1), int(m.group(2))

        words = _tokens(line)
        best, best_score = None, 0.0
        for name in inventory:
            overlap = len(words & _tokens(name)) / max(len(words), 1)
            score = overlap + 0.01 * difflib.SequenceMatcher(None, line.lower(), name.lower()).ratio()
            if score > best_score:
                best, best_score = name, score
        matched = best if best_score >= 0.6 else None

        if matched and price is None:
            price = inventory[matched]["price"]
        rows.append({"description": (matched or line).upper(), "quantity": max(qty, 1), "price": price or 0.0})
    return rows


def whatsapp_link(phone: str, company: str, doc_type: str, doc_num: str, client: str,
                  total: float, bank: dict, due: str | None,
                  advance_pct: float = 0.0) -> str:
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("0"):
        digits = "234" + digits[1:]
    elif len(digits) == 10:                     # 703xxxxxxx typed without the leading 0
        digits = "234" + digits
    msg = (f"Hello *{client}*,\n\nHere is your *{doc_type}* from *{company}*.\n\n"
           f"📄 *Reference:* {doc_num}\n💰 *Total:* ₦{total:,.2f}\n")
    if doc_type == "Invoice" and advance_pct > 0:
        adv = round(total * advance_pct / 100.0, 2)
        msg += (f"🧾 *Advance ({advance_pct:g}%) before work starts:* ₦{adv:,.2f}\n"
                f"Balance ₦{total - adv:,.2f} on completion.\n")
    if doc_type == "Invoice" and due:
        msg += f"📅 *Due:* {due}\n"
    if doc_type == "Invoice" and bank.get("acc_num"):
        msg += (f"\n🏦 *Payment details*\nBank: {bank['bank_name']}\n"
                f"Account number: {bank['acc_num']}\nAccount name: {bank['acc_name']}\n")
    msg += "\nThank you for doing business with us."
    return f"https://api.whatsapp.com/send?phone={digits}&text={urllib.parse.quote(msg)}"


# =========================================================
# 4. PDF ENGINE
# =========================================================
def register_fonts() -> tuple[str, str, str]:
    """Use a TrueType font that has the Naira sign (Helvetica does not, it prints a blank box)."""
    candidates = [
        (FONT_DIR / "NotoSans-Regular.ttf", FONT_DIR / "NotoSans-Bold.ttf"),
        (FONT_DIR / "arial.ttf", FONT_DIR / "arialbd.ttf"),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf")),
        (Path("/Library/Fonts/Arial.ttf"), Path("/Library/Fonts/Arial Bold.ttf")),
        (Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"), Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if not (regular.exists() and bold.exists()):
            continue
        try:
            reg_font = TTFont("OB-Regular", str(regular))
            if 0x20A6 not in reg_font.face.charToGlyph:
                continue
            pdfmetrics.registerFont(reg_font)
            pdfmetrics.registerFont(TTFont("OB-Bold", str(bold)))
            addMapping("OB-Regular", 0, 0, "OB-Regular")
            addMapping("OB-Regular", 1, 0, "OB-Bold")
            addMapping("OB-Regular", 0, 1, "OB-Regular")
            addMapping("OB-Regular", 1, 1, "OB-Bold")
            return "OB-Regular", "OB-Bold", "₦"
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold", "NGN "


FONT, FONT_BOLD, CUR = register_fonts()
BRAND_BLUE = "#2F6BC8"   # medium blue taken from the OT logo (titles, rules, labels)
TINT_BLUE = "#DCE8FA"    # soft fill for table header, totals and payment band
PALE_BLUE = "#F3F7FD"    # very light box background
NAVY = "#1E3A6E"         # text on the soft fills


def accent_for(doc_type: str) -> str:
    return BRAND_BLUE


class NumberedCanvas(canvas.Canvas):
    """Draws the footer and the PAID watermark once the total page count is known."""

    def __init__(self, *args, **kwargs):
        self.meta = kwargs.pop("meta")
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, page_count):
        w, h = self._pagesize
        accent = colors.HexColor(accent_for(self.meta["doc_type"]))
        self.saveState()
        if self.meta["status"] == "PAID":
            self.translate(w / 2, h / 2)
            self.rotate(35)
            self.setFont(FONT_BOLD, 120)
            self.setFillColor(colors.HexColor("#10B981"))
            self.setFillAlpha(0.08)
            self.drawCentredString(0, -40, "PAID")
        self.restoreState()

        self.saveState()
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.75)
        self.line(36, 44, w - 36, 44)
        self.setFont(FONT_BOLD, 7.5)
        self.setFillColor(accent)
        self.drawString(36, 30, self.meta["company"])
        self.setFont(FONT, 7.5)
        self.setFillColor(colors.HexColor("#64748B"))
        self.drawRightString(w - 36, 30, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def _prepared_image(path: Path, ink: bool = False) -> tuple[BytesIO, tuple[int, int]]:
    """Trim the blank margin around an image. For a signature (ink=True) also drop the white
    background so only the strokes remain and they never hide the PAID watermark."""
    with Image.open(path) as im:
        im = im.convert("RGB")
    gray = im.convert("L")
    box = gray.point(lambda v: 255 if v < 240 else 0).getbbox()
    if box:
        pad = 6
        box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
               min(box[2] + pad, im.width), min(box[3] + pad, im.height))
        im, gray = im.crop(box), gray.crop(box)
    if not ink:   # flatten the faint off-white backdrop so no grey box shows around the logo
        white = Image.new("RGB", im.size, (255, 255, 255))
        im = Image.composite(white, im, gray.point(lambda v: 255 if v > 228 else 0))
    if ink:
        out = Image.new("RGBA", im.size, (15, 23, 42, 0))
        out.putalpha(gray.point(lambda v: 255 - v))
        im = out
    buf = BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf, im.size


def _logo_flowable(path: Path, max_w: float, max_h: float, ink: bool = False):
    buf, (iw, ih) = _prepared_image(path, ink)
    scale = min(max_w / iw, max_h / ih)
    img = RLImage(buf, width=iw * scale, height=ih * scale)
    img.hAlign = "LEFT"
    return img


def generate_pdf(p: dict, company: dict, bank: dict,
                 logo_path: Path | None = None, signature_path: Path | None = None):
    """p = {doc_type, doc_num, doc_date, due_date, status, client, items, vat_rate, discount, note}"""
    t = compute_totals(p["items"], p["vat_rate"], p["discount"])
    is_invoice = p["doc_type"] == "Invoice"
    paid = p["status"] == "PAID"

    ACCENT = colors.HexColor(accent_for(p["doc_type"]))
    INK = colors.HexColor("#0F172A")
    MUTED = colors.HexColor("#64748B")
    SOFT = colors.HexColor("#F8FAFC")
    LINE = colors.HexColor("#E2E8F0")

    def style(name, **kw):
        base = dict(fontName=FONT, fontSize=9, leading=12.5, textColor=INK)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_company = style("co", fontName=FONT_BOLD, fontSize=15, leading=18, textColor=ACCENT)
    s_small = style("small", fontSize=8.5, leading=12, textColor=MUTED)
    s_title = style("title", fontName=FONT_BOLD, fontSize=26, leading=28, textColor=ACCENT, alignment=2)
    s_ref = style("ref", fontName=FONT_BOLD, fontSize=10, alignment=2)
    s_label = style("label", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=ACCENT)
    s_body = style("body")
    s_th = style("th", fontName=FONT_BOLD, fontSize=8.5, textColor=colors.HexColor(NAVY))
    s_th_r = style("thr", fontName=FONT_BOLD, fontSize=8.5, textColor=colors.HexColor(NAVY), alignment=2)
    s_td = style("td", fontSize=8.8, leading=11.5)
    s_td_r = style("tdr", fontSize=8.8, leading=11.5, alignment=2)
    s_tot_l = style("totl", textColor=MUTED, alignment=2)
    s_tot_r = style("totr", alignment=2)
    s_grand_l = style("grandl", fontName=FONT_BOLD, fontSize=10, textColor=colors.HexColor(NAVY), alignment=2)
    s_grand_r = style("grandr", fontName=FONT_BOLD, fontSize=11, textColor=colors.HexColor(NAVY), alignment=2)

    esc = xml_escape
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=34, bottomMargin=60,
        title=f"{p['doc_type']} {p['doc_num']}", author=company["company_name"])
    W = doc.width
    story = []

    # --- header: company left, document title right
    left = []
    if logo_path and logo_path.exists():
        left.append(_logo_flowable(logo_path, 2.3 * inch, 1.0 * inch))
        left.append(Spacer(1, 6))
    else:
        left.append(Paragraph(esc(company["company_name"]), s_company))
    left.append(Spacer(1, 3))
    left.append(Paragraph(esc(f"{company['company_addr1']}, {company['company_addr2']}".strip(", ")), s_small))
    phones = " | ".join(company["phones"])
    left.append(Paragraph(esc(f"Tel: {phones}   Email: {company['email']}"), s_small))
    right = [Paragraph(p["doc_type"].upper(), s_title), Spacer(1, 2),
             Paragraph(f"No. {esc(p['doc_num'])}", s_ref)]
    header = Table([[left, right]], colWidths=[W * 0.62, W * 0.38])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, HRFlowable(width="100%", thickness=2, color=ACCENT, spaceBefore=8, spaceAfter=12)]

    # --- billed to / dates
    c = p["client"]
    billed = [Paragraph("BILLED TO", s_label), Spacer(1, 3),
              Paragraph(f"<b>{esc(c['name'] or '—')}</b>", s_body)]
    for line in (c["address"], ", ".join(x for x in [c["city"], c["country"]] if x), c["phone"]):
        if line:
            billed.append(Paragraph(esc(line), s_body))
    details = [Paragraph("DETAILS", s_label), Spacer(1, 3),
               Paragraph(f"<b>Issued:</b> {p['doc_date'].strftime('%d %B %Y')}", s_body)]
    if is_invoice and p.get("due_date"):
        details.append(Paragraph(f"<b>Due:</b> {p['due_date'].strftime('%d %B %Y')}", s_body))
    details.append(Paragraph(f"<b>Status:</b> {'Paid' if paid else 'Awaiting payment'}", s_body))
    info = Table([[billed, details]], colWidths=[W * 0.58, W * 0.42])
    info.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("LINEBEFORE", (0, 0), (0, 0), 2.5, ACCENT),
        ("LINEBEFORE", (1, 0), (1, 0), 0.75, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [info, Spacer(1, 14)]

    # --- line items
    rows = [[Paragraph("Description", s_th), Paragraph("Qty", s_th_r),
             Paragraph(f"Unit price ({CUR.strip()})", s_th_r), Paragraph(f"Amount ({CUR.strip()})", s_th_r)]]
    for it in p["items"]:
        rows.append([Paragraph(esc(it["description"]), s_td), Paragraph(str(it["quantity"]), s_td_r),
                     Paragraph(f"{it['price']:,.2f}", s_td_r),
                     Paragraph(f"{it['quantity'] * it['price']:,.2f}", s_td_r)])
    items_tbl = Table(rows, colWidths=[W * 0.50, W * 0.10, W * 0.20, W * 0.20], repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TINT_BLUE)),
        ("LINEABOVE", (0, 0), (-1, 0), 1.5, ACCENT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    story += [items_tbl, Spacer(1, 10)]

    # --- words + totals
    tot_rows = [[Paragraph("Subtotal", s_tot_l), Paragraph(f"{CUR}{t['subtotal']:,.2f}", s_tot_r)]]
    if t["discount"] > 0:
        tot_rows.append([Paragraph("Discount", s_tot_l), Paragraph(f"-{CUR}{t['discount']:,.2f}", s_tot_r)])
    if t["vat_rate"] > 0:
        tot_rows.append([Paragraph(f"VAT ({t['vat_rate']:g}%)", s_tot_l), Paragraph(f"{CUR}{t['vat']:,.2f}", s_tot_r)])
    grand_label = "Total paid" if (not is_invoice or paid) else "Total due"
    tot_rows.append([Paragraph(grand_label, s_grand_l), Paragraph(f"{CUR}{t['total']:,.2f}", s_grand_r)])
    totals_tbl = Table(tot_rows, colWidths=[W * 0.22, W * 0.26])
    totals_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor(TINT_BLUE)),
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, -1), (-1, -1), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))

    words = [Paragraph("AMOUNT IN WORDS", s_label), Spacer(1, 3),
             Paragraph(esc(amount_to_words(t["total"])), s_body)]
    if p.get("note"):
        words += [Spacer(1, 8), Paragraph("NOTE", s_label), Spacer(1, 3), Paragraph(esc(p["note"]), s_small)]
    summary = Table([[words, totals_tbl]], colWidths=[W * 0.52, W * 0.48])
    summary.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 14),
                                 ("RIGHTPADDING", (1, 0), (1, 0), 0)]))

    # --- advance payment terms (invoices for services)
    terms_box = None
    adv_pct = float(p.get("advance_pct") or 0.0)
    if is_invoice and not paid and adv_pct > 0:
        adv = round(t["total"] * adv_pct / 100.0, 2)
        bal = round(t["total"] - adv, 2)
        terms = [Paragraph("PAYMENT TERMS", s_label), Spacer(1, 3),
                 Paragraph(f"<b>{adv_pct:g}% advance payment ({CUR}{adv:,.2f})</b> is required before commencement "
                           f"of service. The balance of <b>{CUR}{bal:,.2f}</b> is due on completion.", s_body)]
        terms_box = Table([[terms]], colWidths=[W])
        terms_box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(PALE_BLUE)),
                                       ("LINEBEFORE", (0, 0), (0, 0), 2.5, ACCENT),
                                       ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12)]))

    # --- payment details + signature
    s_pay_head = style("payhead", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=ACCENT)
    s_pay_lbl = style("paylbl", fontName=FONT_BOLD, fontSize=6.5, leading=8.5, textColor=colors.HexColor("#5A7FBF"))
    s_pay_val = style("payval", fontName=FONT_BOLD, fontSize=9, leading=11.5, textColor=colors.HexColor(NAVY))
    s_pay_acct = style("payacct", fontName=FONT_BOLD, fontSize=14, leading=17, textColor=colors.HexColor(NAVY))
    s_pay_hint = style("payhint", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#35517F"))

    pay_rows = [
        [Paragraph("PAYMENT DETAILS", s_pay_head), "", ""],
        [[Paragraph("BANK", s_pay_lbl), Paragraph(esc(bank["bank_name"]), s_pay_val)],
         [Paragraph("ACCOUNT NUMBER", s_pay_lbl), Paragraph(esc(bank["acc_num"]), s_pay_acct)],
         [Paragraph("ACCOUNT NAME", s_pay_lbl), Paragraph(esc(bank["acc_name"]), s_pay_val)]],
    ]
    pay_style = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(TINT_BLUE)),
        ("LINEABOVE", (0, 0), (-1, 0), 3, ACCENT),
        ("SPAN", (0, 0), (-1, 0)),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#B7CBEC")),
        ("VALIGN", (0, 1), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, 1), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    if is_invoice and not paid:
        pay_rows.append([Paragraph(f"Please use <b>{esc(p['doc_num'])}</b> as the payment reference.", s_pay_hint), "", ""])
        pay_style += [("SPAN", (0, 2), (-1, 2)), ("TOPPADDING", (0, 2), (-1, 2), 0), ("BOTTOMPADDING", (0, 2), (-1, 2), 6)]
    bank_box = Table(pay_rows, colWidths=[W * 0.20, W * 0.34, W * 0.46])
    bank_box.setStyle(TableStyle(pay_style))
    if is_invoice:                       # invoices carry no signature, only receipts do
        sig_box = Spacer(1, 1)
    else:
        sig = []
        if signature_path and signature_path.exists():
            sig.append(_logo_flowable(signature_path, 1.7 * inch, 0.55 * inch, ink=True))
        else:
            sig.append(Spacer(1, 30))
        sig += [HRFlowable(width="100%", thickness=0.75, color=MUTED, spaceBefore=3, spaceAfter=3),
                Paragraph("Authorised signatory", style("sig", fontSize=7.5, textColor=MUTED))]
        sig_box = Table([[sig]], colWidths=[W * 0.34])
        sig_box.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    if is_invoice:                       # bank details on invoices only; receipts show the signature instead
        footer_parts = [bank_box]
    else:
        sig_row = Table([["", sig_box]], colWidths=[W * 0.64, W * 0.36])
        sig_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        footer_parts = [sig_row]
    story.append(KeepTogether([summary, Spacer(1, 14)] + ([terms_box, Spacer(1, 10)] if terms_box else []) + footer_parts))

    meta = {"doc_type": p["doc_type"], "status": p["status"], "company": company["company_name"]}
    doc.build(story, canvasmaker=lambda *a, **k: NumberedCanvas(*a, meta=meta, **k))
    return buf.getvalue(), t


# ===== UI =====
# =========================================================
# 5. STREAMLIT UI
# =========================================================
PAGES = ["New document", "History", "Catalogue", "Settings"]
NEW_CLIENT = "New client"


def require_login() -> None:
    password = os.environ.get("APP_PASSWORD")
    if not password:
        try:
            password = st.secrets.get("APP_PASSWORD")
        except Exception:
            password = None
    if not password or st.session_state.get("authed"):
        return
    st.markdown("### Sign in")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            if hmac.compare_digest(entered.encode(), str(password).encode()):
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def money(x: float) -> str:
    return f"₦{x:,.2f}"


def inject_css(accent: str, soft: str) -> None:
    st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&display=swap');
:root {{ --accent:{accent}; --accent-soft:{soft}; --ink:#0F172A; --muted:#64748B; --line:#E2E8F0; }}
.stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp [data-testid="stMarkdownContainer"] {{
    font-family:'Figtree', system-ui, -apple-system, 'Segoe UI', sans-serif; }}
#MainMenu, footer {{ visibility:hidden; }}
[data-testid="stHeader"] {{ background:transparent; }}
.block-container {{ padding-top:1.2rem; padding-bottom:3rem; max-width:1280px; }}

/* masthead */
.ob-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap:24px; flex-wrap:wrap;
    padding-bottom:14px; margin-bottom:14px; border-bottom:3px solid var(--accent); }}
.ob-co {{ font-size:1.55rem; font-weight:700; letter-spacing:-0.01em; color:var(--ink); line-height:1.1; }}
.ob-sub {{ color:var(--muted); font-size:.9rem; margin-top:2px; }}
.ob-kpis {{ display:flex; gap:28px; flex-wrap:wrap; }}
.ob-kpi span {{ display:block; color:var(--muted); font-size:.78rem; }}
.ob-kpi b {{ display:block; font-size:1.25rem; font-weight:700; color:var(--ink); font-variant-numeric:tabular-nums; }}
.ob-kpi i {{ display:block; font-style:normal; color:var(--muted); font-size:.75rem; }}
.ob-kpi.warn b {{ color:#B45309; }}

/* cards + section titles */
[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius:12px; border-color:var(--line); }}
.ob-h {{ font-weight:700; font-size:1.02rem; color:var(--ink); margin:0 0 .35rem; }}
.ob-note {{ color:var(--muted); font-size:.85rem; }}

/* the document preview is the hero */
[data-testid="stImage"] img {{ border:1px solid var(--line); border-top:5px solid var(--accent);
    border-radius:4px; box-shadow:0 12px 32px -12px rgba(15,23,42,.28); }}
.ob-totals {{ width:100%; border-collapse:collapse; margin:.2rem 0 .6rem; font-variant-numeric:tabular-nums; }}
.ob-totals td {{ padding:3px 0; font-size:.92rem; color:var(--muted); }}
.ob-totals td:last-child {{ text-align:right; color:var(--ink); }}
.ob-totals tr.grand td {{ border-top:1px solid var(--line); padding-top:8px; font-weight:700;
    font-size:1.1rem; color:var(--ink); }}
.ob-totals tr.grand td:last-child {{ color:var(--accent); }}
div[data-testid="stColumn"]:has(.ob-sticky) {{ position:sticky; top:3.2rem; align-self:flex-start; }}

/* controls */
button[data-testid="stBaseButton-primary"] {{ background:var(--accent); border-color:var(--accent); font-weight:600; }}
button[data-testid="stBaseButton-primary"]:hover {{ filter:brightness(.92); background:var(--accent); border-color:var(--accent); }}
.stButton button, .stDownloadButton button, .stLinkButton a {{ border-radius:8px; min-height:42px; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea {{ border-radius:8px; }}
@media (max-width: 900px) {{ div[data-testid="stColumn"]:has(.ob-sticky) {{ position:static; }} }}
</style>
""", unsafe_allow_html=True)


# ---------- session state ----------
def init_state(settings: dict) -> None:
    ss = st.session_state
    today = date.today()
    ss.setdefault("nav", PAGES[0])
    ss.setdefault("doc_type", "Invoice")
    ss.setdefault("doc_num", get_next_ref_num("Invoice"))
    ss.setdefault("is_paid", False)
    ss.setdefault("deduct_stock", True)
    ss.setdefault("doc_date", today)
    ss.setdefault("due_date", today + timedelta(days=int(settings["due_days"])))
    ss.setdefault("vat_on", False)
    ss.setdefault("adv_on", False)
    ss.setdefault("adv_pct", float(settings["advance_pct"]))
    ss.setdefault("discount", 0.0)
    ss.setdefault("doc_note", settings["default_note"])
    ss.setdefault("cl_pick", NEW_CLIENT)
    ss.setdefault("cl_name", "")
    ss.setdefault("cl_addr", "")
    ss.setdefault("cl_city", "Lagos")
    ss.setdefault("cl_country", "Nigeria")
    ss.setdefault("cl_phone", "")
    ss.setdefault("cl_save", True)
    ss.setdefault("items_df", items_to_df([]))
    ss.setdefault("items_ver", 0)
    ss.setdefault("source_invoice", None)
    ss.setdefault("add_pick", None)
    ss.setdefault("add_desc", "")
    ss.setdefault("add_price", 0.0)
    ss.setdefault("add_qty", 1)


def apply_pending(settings: dict) -> None:
    """Changes requested from another page. Must run before the widgets exist."""
    ss = st.session_state
    if ss.pop("_reset_add", False):
        ss.add_pick, ss.add_desc, ss.add_price, ss.add_qty = None, "", 0.0, 1
    p = ss.pop("pending", None)
    if not p:
        return
    dt = p.get("doc_type", "Invoice")
    cl = p.get("client", {})
    ss.nav = p.get("nav", "New document")
    ss.doc_type = dt
    ss.doc_num = get_next_ref_num(dt)
    ss.is_paid = bool(p.get("is_paid", dt == "Receipt"))
    ss.deduct_stock = bool(p.get("deduct_stock", dt == "Invoice"))
    ss.doc_date = date.today()
    ss.due_date = date.today() + timedelta(days=int(settings["due_days"]))
    ss.vat_on = bool(p.get("vat_on", False))
    ss.adv_on = float(p.get("advance_pct", 0) or 0) > 0 and dt == "Invoice"
    ss.adv_pct = float(p.get("advance_pct") or settings["advance_pct"])
    ss.discount = float(p.get("discount", 0.0))
    ss.doc_note = p.get("note", settings["default_note"])
    ss.cl_pick = cl.get("name") if cl.get("name") in get_clients() else NEW_CLIENT
    ss.cl_name = cl.get("name", "")
    ss.cl_addr = cl.get("address", "")
    ss.cl_city = cl.get("city", "Lagos" if not cl else "")
    ss.cl_country = cl.get("country", "Nigeria" if not cl else "")
    ss.cl_phone = cl.get("phone", "")
    ss.items_df = items_to_df(p.get("items", []))
    ss.items_ver += 1
    ss.source_invoice = p.get("source_invoice")


def on_type_change() -> None:
    ss = st.session_state
    ss.doc_num = get_next_ref_num(ss.doc_type)
    ss.is_paid = ss.doc_type == "Receipt"
    ss.deduct_stock = ss.doc_type == "Invoice"
    if ss.doc_type != "Invoice":
        ss.adv_on = False
    if ss.doc_type == "Invoice":
        ss.source_invoice = None


def on_client_pick() -> None:
    ss = st.session_state
    c = get_clients().get(ss.cl_pick)
    if c:
        ss.cl_name, ss.cl_addr, ss.cl_city = ss.cl_pick, c["address"], c["city"]
        ss.cl_country, ss.cl_phone = c["country"], c["phone"]
    else:
        ss.cl_name, ss.cl_addr, ss.cl_city, ss.cl_country, ss.cl_phone = "", "", "Lagos", "Nigeria", ""


def on_catalog_pick() -> None:
    """Runs when an item is chosen (or a new name typed) in the search box."""
    ss = st.session_state
    pick = (ss.add_pick or "").strip().upper()
    if not pick:
        ss.add_desc, ss.add_price, ss.add_qty = "", 0.0, 1
        return
    sugg = get_suggestions()
    known = sugg.get(pick)
    price = float(known["price"]) if known else 0.0
    if price == 0.0:   # a variant with no price yet starts from its base model (the longest name it extends)
        bases = [n for n, v in sugg.items() if v["price"] > 0 and pick.startswith(n)]
        if bases:
            price = float(sugg[max(bases, key=len)]["price"])
    ss.add_desc, ss.add_price = pick, price
    ss.add_qty = 1


def flash(kind: str, text: str) -> None:
    st.session_state["_flash"] = (kind, text)


def show_flash() -> None:
    f = st.session_state.pop("_flash", None)
    if f:
        {"success": st.success, "error": st.error, "info": st.info, "warning": st.warning}[f[0]](f[1], icon=None)


# ---------- masthead ----------
def masthead(settings: dict, hist: pd.DataFrame, inventory: dict) -> None:
    unpaid_total, unpaid_n, month_total = 0.0, 0, 0.0
    if not hist.empty:
        unpaid = hist[(hist["doc_type"] == "Invoice") & (hist["status"] != "PAID")]
        unpaid_total, unpaid_n = float(unpaid["total_amount"].sum()), len(unpaid)
        rev = revenue_rows(hist)
        now = pd.Timestamp.now()
        in_month = rev[(rev["created_at"].dt.year == now.year) & (rev["created_at"].dt.month == now.month)]
        month_total = float(in_month["total_amount"].sum())
    low = [n for n, i in inventory.items() if i["stock"] <= int(settings["low_stock_at"])]
    low_cls = " warn" if low else ""
    st.markdown(f"""
<div class="ob-head">
  <div><div class="ob-co">{html.escape(settings['company_name'])}</div>
       <div class="ob-sub">Invoices and receipts</div></div>
  <div class="ob-kpis">
    <div class="ob-kpi"><span>Waiting to be paid</span><b>{money(unpaid_total)}</b>
         <i>{unpaid_n} unpaid invoice{'s' if unpaid_n != 1 else ''}</i></div>
    <div class="ob-kpi"><span>Collected this month</span><b>{money(month_total)}</b></div>
    <div class="ob-kpi{low_cls}"><span>Low on stock</span><b>{len(low)} item{'s' if len(low) != 1 else ''}</b>
         <i>{html.escape(', '.join(low[:2]))}{'…' if len(low) > 2 else ''}</i></div>
  </div>
</div>""", unsafe_allow_html=True)


# ---------- new document ----------
@st.cache_data(show_spinner=False, max_entries=16)
def render_pages(pdf_bytes: bytes, scale: float = 1.6) -> list[bytes]:
    pdf = pdfium.PdfDocument(pdf_bytes)
    pages = []
    for i in range(len(pdf)):
        buf = BytesIO()
        pdf[i].render(scale=scale).to_pil().save(buf, "PNG")
        pages.append(buf.getvalue())
    return pages


def company_and_bank(settings: dict) -> tuple[dict, dict]:
    company = {k: settings[k] for k in ("company_name", "company_addr1", "company_addr2",
                                        "phones", "email")}
    bank = {k: settings[k] for k in ("bank_name", "acc_num", "acc_name")}
    return company, bank


def page_new_document(settings: dict) -> None:
    ss = st.session_state
    inventory = get_inventory()
    suggestions = get_suggestions()
    clients = get_clients()
    inv_upper = {k.upper(): v for k, v in inventory.items()}
    company, bank = company_and_bank(settings)

    left, right = st.columns([1.05, 1], gap="large")

    # ----- left: the form -----
    with left:
        with st.container(border=True):
            st.markdown('<div class="ob-h">Document</div>', unsafe_allow_html=True)
            st.radio("Type", ["Invoice", "Receipt"], key="doc_type", horizontal=True,
                     on_change=on_type_change, label_visibility="collapsed")
            c1, c2 = st.columns(2)
            c1.text_input("Reference number", key="doc_num")
            c2.date_input("Issue date", key="doc_date", format="DD/MM/YYYY")
            if ss.doc_type == "Invoice":
                c3, c4 = st.columns(2)
                c3.date_input("Due date", key="due_date", format="DD/MM/YYYY")
                c4.toggle("Already paid", key="is_paid")
            if ss.source_invoice:
                st.caption(f"Receipt for invoice {ss.source_invoice}. That invoice is marked paid when you save.")

        with st.container(border=True):
            st.markdown('<div class="ob-h">Client</div>', unsafe_allow_html=True)
            st.selectbox("Saved clients", [NEW_CLIENT] + list(clients), key="cl_pick",
                         on_change=on_client_pick)
            st.text_input("Name", key="cl_name", placeholder="Client or business name")
            st.text_input("Address", key="cl_addr")
            c1, c2 = st.columns(2)
            c1.text_input("City / state", key="cl_city")
            c2.text_input("Country", key="cl_country")
            st.text_input("Phone (for WhatsApp)", key="cl_phone", placeholder="0703 000 0000")
            st.checkbox("Remember this client", key="cl_save")

        with st.container(border=True):
            st.markdown('<div class="ob-h">Items</div>', unsafe_allow_html=True)
            add_box = st.container()
            editor_box = st.container()

            with editor_box:
                edited = st.data_editor(
                    ss.items_df, key=f"items_editor_{ss.items_ver}", num_rows="dynamic",
                    hide_index=True, width="stretch",
                    column_config={
                        "description": st.column_config.TextColumn("Description", required=True, width="large"),
                        "quantity": st.column_config.NumberColumn("Qty", min_value=1, step=1, default=1, required=True, width="small"),
                        "price": st.column_config.NumberColumn("Unit price", min_value=0.0, default=0.0, format="₦%.2f", required=True),
                    })
            items = clean_items(edited)

            def append_items(new_rows: list[dict]) -> None:
                ss.items_df = items_to_df(items + new_rows)
                ss.items_ver += 1
                ss["_reset_add"] = True
                st.rerun()

            with add_box:
                st.selectbox(
                    "Find an item", list(suggestions), index=None, key="add_pick",
                    accept_new_options=True, on_change=on_catalog_pick,
                    placeholder="Start typing, e.g. desktop core i3",
                    help="Matching items from your catalogue and past documents appear as you type. "
                         "Pick one, or press Enter to use what you typed as a new item.")
                picked = suggestions.get((ss.add_pick or "").strip().upper())
                if picked and picked.get("stock") is not None:
                    st.caption(f"In stock: {picked['stock']}")
                elif picked:
                    st.caption("Not in stock list. Set the price below if it shows 0.")
                b1, b2, b3 = st.columns([2.4, 1.2, 0.8])
                b1.text_input("Description (you can edit it)", key="add_desc", placeholder="e.g. Core i5 desktop")
                b2.number_input("Unit price", min_value=0.0, step=1000.0, key="add_price")
                b3.number_input("Qty", min_value=1, step=1, key="add_qty")
                if st.button("Add item", width="stretch"):
                    if ss.add_desc.strip():
                        append_items([{"description": ss.add_desc.strip().upper(),
                                       "quantity": int(ss.add_qty), "price": float(ss.add_price)}])
                    else:
                        st.warning("Choose or type an item first.")
                with st.expander("Add several items from text"):
                    st.caption("One per line, for example: 2pcs 512GB nvme ssd, cat6 cable x3, router @ 95k")
                    pasted = st.text_area("Items", key="paste_text", height=90, label_visibility="collapsed")
                    if st.button("Add these lines", key="paste_btn"):
                        rows = parse_quick_items(pasted, suggestions)
                        if rows:
                            append_items(rows)
                        st.warning("Nothing to add.")

            problems = []
            for it in items:
                stock = inv_upper.get(it["description"], {}).get("stock")
                if stock is not None and it["quantity"] > stock and ss.doc_type == "Invoice":
                    problems.append(f"{it['description']}: {it['quantity']} requested, {stock} in stock")
            if problems:
                st.warning("Not enough stock for: " + "; ".join(problems))

        with st.container(border=True):
            st.markdown('<div class="ob-h">Totals and notes</div>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            c1.toggle(f"Add VAT ({settings['vat_rate']:g}%)", key="vat_on")
            c2.number_input("Discount (₦)", min_value=0.0, step=500.0, key="discount")
            if ss.doc_type == "Invoice":
                a1, a2 = st.columns(2)
                a1.toggle("Service: advance payment before work starts", key="adv_on")
                if ss.adv_on:
                    a2.number_input("Advance (%)", min_value=1.0, max_value=100.0, step=5.0, key="adv_pct")
            st.text_area("Note on the document", key="doc_note", height=70)
            st.toggle("Take sold items out of stock when saved", key="deduct_stock")

    # ----- right: live preview and save -----
    with right:
        st.markdown('<span class="ob-sticky"></span>', unsafe_allow_html=True)
        status = "PAID" if (ss.doc_type == "Receipt" or ss.is_paid) else "BLANK"
        vat_rate = float(settings["vat_rate"]) if ss.vat_on else 0.0
        totals = compute_totals(items, vat_rate, float(ss.discount))

        saved = ss.get("last_saved")
        if saved:
            with st.container(border=True):
                st.markdown(f"**{saved['doc_type']} {saved['doc_num']} saved.**")
                s1, s2 = st.columns(2)
                s1.download_button("Download PDF", saved["pdf"], file_name=f"{saved['doc_type']}_{saved['doc_num']}.pdf",
                                   mime="application/pdf", type="primary", width="stretch")
                if saved["phone"]:
                    s2.link_button("Send on WhatsApp", saved["wa"], width="stretch")
                else:
                    s2.caption("Add a client phone number to send on WhatsApp.")
                if st.button("Close", key="close_saved"):
                    ss.pop("last_saved")
                    st.rerun()

        if not items:
            st.info("Add an item and the document appears here as you type.")
        else:
            payload = {
                "doc_type": ss.doc_type, "doc_num": ss.doc_num.strip() or "—", "doc_date": ss.doc_date,
                "due_date": ss.due_date if ss.doc_type == "Invoice" else None, "status": status,
                "client": {"name": ss.cl_name.strip(), "address": ss.cl_addr, "city": ss.cl_city,
                           "country": ss.cl_country, "phone": ss.cl_phone},
                "items": items, "vat_rate": vat_rate, "discount": float(ss.discount),
                "note": ss.doc_note.strip(),
                "advance_pct": float(ss.adv_pct) if (ss.doc_type == "Invoice" and ss.adv_on) else 0.0,
            }
            pdf_bytes, totals = generate_pdf(payload, company, bank,
                                             LOGO_FILE if LOGO_FILE.exists() else None,
                                             SIGNATURE_FILE if SIGNATURE_FILE.exists() else None)
            pages = render_pages(pdf_bytes)
            page_no = 0
            if len(pages) > 1:
                page_no = st.radio("Page", range(len(pages)), format_func=lambda i: f"Page {i + 1}",
                                   horizontal=True, label_visibility="collapsed") 
            st.image(pages[page_no], width="stretch")

            rows = f"<tr><td>Subtotal</td><td>{money(totals['subtotal'])}</td></tr>"
            if totals["discount"]:
                rows += f"<tr><td>Discount</td><td>-{money(totals['discount'])}</td></tr>"
            if totals["vat_rate"]:
                rows += f"<tr><td>VAT ({totals['vat_rate']:g}%)</td><td>{money(totals['vat'])}</td></tr>"
            rows += f"<tr class='grand'><td>Total</td><td>{money(totals['total'])}</td></tr>"
            st.markdown(f"<table class='ob-totals'>{rows}</table>", unsafe_allow_html=True)

            missing = []
            if not ss.cl_name.strip():
                missing.append("a client name")
            if not ss.doc_num.strip():
                missing.append("a reference number")
            if missing:
                st.caption("Still needed: " + " and ".join(missing) + ".")
            if st.button(f"Save {ss.doc_type.lower()}", type="primary", width="stretch",
                         disabled=bool(missing)):
                client = {"name": ss.cl_name.strip(), "address": ss.cl_addr, "city": ss.cl_city,
                          "country": ss.cl_country, "phone": ss.cl_phone}
                meta = {"client": client, "vat_rate": vat_rate, "discount": totals["discount"],
                        "doc_date": ss.doc_date.isoformat(),
                        "due_date": ss.due_date.isoformat() if ss.doc_type == "Invoice" else None,
                        "note": ss.doc_note.strip(), "source_invoice": ss.source_invoice,
                        "advance_pct": payload["advance_pct"]}
                try:
                    save_document(ss.doc_type, ss.doc_num.strip(), client, items, totals["total"],
                                  status, meta, ss.deduct_stock, ss.cl_save)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    due_txt = ss.due_date.strftime("%d %B %Y") if ss.doc_type == "Invoice" else None
                    ss["last_saved"] = {
                        "doc_type": ss.doc_type, "doc_num": ss.doc_num.strip(), "pdf": pdf_bytes,
                        "phone": client["phone"],
                        "wa": whatsapp_link(client["phone"], company["company_name"], ss.doc_type,
                                            ss.doc_num.strip(), client["name"], totals["total"], bank, due_txt,
                                            payload["advance_pct"])
                        if client["phone"] else "",
                    }
                    ss["pending"] = {"doc_type": ss.doc_type, "nav": "New document"}
                    st.rerun()


# ---------- history ----------
def doc_payload_from_row(row, clients: dict, settings: dict) -> dict:
    meta = row["meta"] if isinstance(row["meta"], dict) else {}
    items = safe_json(row["items_json"], [])
    client = meta.get("client") or {"name": row["client_name"], **clients.get(row["client_name"], {})}
    client = {k: client.get(k, "") for k in ("name", "address", "city", "country", "phone")}
    d = lambda s: datetime.fromisoformat(s).date() if s else None
    created = row["created_at"].date() if pd.notna(row["created_at"]) else date.today()
    return {
        "doc_type": row["doc_type"], "doc_num": row["doc_num"],
        "doc_date": d(meta.get("doc_date")) or created, "due_date": d(meta.get("due_date")),
        "status": row["status"], "client": client, "items": items,
        "vat_rate": float(meta.get("vat_rate", 0.0)), "discount": float(meta.get("discount", 0.0)),
        "note": meta.get("note", ""), "advance_pct": float(meta.get("advance_pct") or 0.0),
    }


def page_history(settings: dict, hist: pd.DataFrame) -> None:
    ss = st.session_state
    if hist.empty:
        st.info("Nothing here yet. Save an invoice or receipt and it will show up in this list.")
        return

    company, bank = company_and_bank(settings)
    clients = get_clients()

    f1, f2, f3 = st.columns([2, 1, 1])
    query = f1.text_input("Search", placeholder="Client name or reference", label_visibility="collapsed")
    kind = f2.selectbox("Type", ["All types", "Invoice", "Receipt"], label_visibility="collapsed")
    state = f3.selectbox("Status", ["All statuses", "Unpaid", "Paid"], label_visibility="collapsed")

    view = hist.copy()
    if query:
        view = view[view["client_name"].str.contains(query, case=False, na=False)
                    | view["doc_num"].str.contains(query, case=False, na=False)]
    if kind != "All types":
        view = view[view["doc_type"] == kind]
    if state != "All statuses":
        view = view[(view["status"] == "PAID") == (state == "Paid")]

    rev = revenue_rows(view)
    m1, m2, m3 = st.columns(3)
    m1.metric("Collected (shown)", money(float(rev["total_amount"].sum())))
    m2.metric("Unpaid invoices (shown)", money(float(view[(view["doc_type"] == "Invoice") & (view["status"] != "PAID")]["total_amount"].sum())))
    m3.metric("Documents shown", len(view))

    table = pd.DataFrame({
        "Reference": view["doc_num"], "Type": view["doc_type"], "Client": view["client_name"],
        "Amount": view["total_amount"], "Status": view["status"].map(lambda s: "Paid" if s == "PAID" else "Unpaid"),
        "Date": view["created_at"].dt.strftime("%d %b %Y"),
    })
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key="hist_table", column_config={"Amount": st.column_config.NumberColumn(format="₦%.2f")})

    csv = table.to_csv(index=False).encode("utf-8")
    st.download_button("Export list as CSV", csv, file_name="document_history.csv", mime="text/csv")

    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.caption("Select a row to download it, send it, or turn an invoice into a receipt.")
        return
    row = view.iloc[rows[0]]
    payload = doc_payload_from_row(row, clients, settings)
    totals = compute_totals(payload["items"], payload["vat_rate"], payload["discount"])

    with st.container(border=True):
        st.markdown(f"<div class='ob-h'>{html.escape(row['doc_num'])} · {html.escape(row['client_name'])}</div>",
                    unsafe_allow_html=True)
        st.dataframe(items_to_df(payload["items"]).rename(columns={
            "description": "Description", "quantity": "Qty", "price": "Unit price"}),
            hide_index=True, width="stretch",
            column_config={"Unit price": st.column_config.NumberColumn(format="₦%.2f")})
        if abs(totals["total"] - float(row["total_amount"])) > 0.01:
            st.caption("This record was saved by an older version, so VAT and discount were not stored. "
                       "The PDF total may differ from the amount in the list.")

        a1, a2, a3, a4 = st.columns(4)
        pdf_bytes, _ = generate_pdf(payload, company, bank,
                                    LOGO_FILE if LOGO_FILE.exists() else None,
                                    SIGNATURE_FILE if SIGNATURE_FILE.exists() else None)
        a1.download_button("Download PDF", pdf_bytes, file_name=f"{row['doc_type']}_{row['doc_num']}.pdf",
                           mime="application/pdf", type="primary", width="stretch")
        phone = payload["client"]["phone"]
        if phone:
            due_txt = payload["due_date"].strftime("%d %B %Y") if payload["due_date"] else None
            a2.link_button("WhatsApp", whatsapp_link(phone, company["company_name"], row["doc_type"], row["doc_num"],
                                                     row["client_name"], float(row["total_amount"]), bank, due_txt,
                                                     payload["advance_pct"]),
                           width="stretch")
        else:
            a2.button("WhatsApp", disabled=True, help="No phone number saved for this client",
                      width="stretch")

        load = {"client": payload["client"], "items": payload["items"],
                "vat_rate": payload["vat_rate"], "discount": payload["discount"], "note": payload["note"],
                "advance_pct": payload["advance_pct"]}
        if row["doc_type"] == "Invoice" and row["status"] != "PAID":
            if a3.button("Create receipt", width="stretch"):
                ss["pending"] = {**load, "doc_type": "Receipt", "is_paid": True, "deduct_stock": False,
                                 "vat_on": payload["vat_rate"] > 0, "source_invoice": row["doc_num"]}
                st.rerun()
            if a4.button("Mark as paid", width="stretch"):
                set_document_status(row["doc_num"], "PAID")
                st.rerun()
        else:
            if a3.button("Copy to new document", width="stretch"):
                ss["pending"] = {**load, "doc_type": row["doc_type"], "vat_on": payload["vat_rate"] > 0,
                                 "is_paid": False, "deduct_stock": row["doc_type"] == "Invoice"}
                st.rerun()

        with st.popover("Delete this record"):
            st.write("This cannot be undone.")
            put_back = st.checkbox("Return the items to stock", value=row["doc_type"] == "Invoice")
            if st.button("Delete", type="primary", key=f"del_{row['id']}"):
                delete_document(int(row["id"]), put_back)
                flash("success", f"Deleted {row['doc_num']}.")
                st.rerun()


# ---------- catalogue ----------
def page_catalogue(settings: dict) -> None:
    tab_stock, tab_sugg = st.tabs(["Stock and prices", "Suggestions"])

    with tab_stock:
        inventory = get_inventory()
        st.caption("Items you keep in stock. Edit in place, add a row at the bottom, or select a row and press "
                   "Delete. Press Save when you are done.")
        df = pd.DataFrame([{"item_name": k, "default_price": v["price"], "stock_qty": v["stock"]}
                           for k, v in inventory.items()])
        edited = st.data_editor(
            df, num_rows="dynamic", hide_index=True, width="stretch", key="catalogue_editor",
            column_config={
                "item_name": st.column_config.TextColumn("Item", required=True, width="large"),
                "default_price": st.column_config.NumberColumn("Default price", min_value=0.0, format="₦%.2f", required=True),
                "stock_qty": st.column_config.NumberColumn("In stock", min_value=0, step=1, required=True),
            })
        low = [n for n, i in inventory.items() if i["stock"] <= int(settings["low_stock_at"])]
        if low:
            st.warning(f"At or below {settings['low_stock_at']} units: " + ", ".join(low))
        if st.button("Save catalogue", type="primary"):
            n = replace_inventory(edited)
            flash("success", f"Catalogue saved ({n} items).")
            st.rerun()

    with tab_sugg:
        st.caption("Descriptions offered as you type in the Find an item box. These are not stock items, so "
                   "services and one-off products belong here. Anything you invoice is also remembered "
                   "automatically. Add your own, edit, or delete rows, then press Save.")
        lib = get_library()
        ldf = pd.DataFrame([{"name": k, "price": v} for k, v in lib.items()])
        ledit = st.data_editor(
            ldf, num_rows="dynamic", hide_index=True, width="stretch", key="library_editor",
            column_config={
                "name": st.column_config.TextColumn("Description", required=True, width="large"),
                "price": st.column_config.NumberColumn("Usual price (optional)", min_value=0.0, format="₦%.2f"),
            })
        if st.button("Save suggestions", type="primary"):
            n = replace_library(ledit)
            flash("success", f"Suggestions saved ({n} descriptions).")
            st.rerun()


# ---------- settings ----------
def page_settings(settings: dict) -> None:
    with st.container(border=True):
        st.markdown('<div class="ob-h">Business</div>', unsafe_allow_html=True)
        name = st.text_input("Company name", settings["company_name"])
        c1, c2 = st.columns(2)
        a1 = c1.text_input("Address line 1", settings["company_addr1"])
        a2 = c2.text_input("Address line 2", settings["company_addr2"])
        st.caption("Phone numbers printed on documents. Add a row at the bottom for another number.")
        phones_df = st.data_editor(
            pd.DataFrame({"Phone number": settings["phones"] or [""]}), num_rows="dynamic",
            hide_index=True, width="stretch", key="phones_editor",
            column_config={"Phone number": st.column_config.TextColumn("Phone number", required=True)})
        email = st.text_input("Email", settings["email"])

    with st.container(border=True):
        st.markdown('<div class="ob-h">Payment details</div>', unsafe_allow_html=True)
        st.caption("Printed on every document and sent in the WhatsApp message for invoices.")
        c1, c2, c3 = st.columns(3)
        bank = c1.text_input("Bank", settings["bank_name"])
        acc = c2.text_input("Account number", settings["acc_num"])
        acc_name = c3.text_input("Account name", settings["acc_name"])

    with st.container(border=True):
        st.markdown('<div class="ob-h">Defaults</div>', unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        vat = c1.number_input("VAT rate (%)", min_value=0.0, max_value=100.0, step=0.5, value=float(settings["vat_rate"]))
        due = c2.number_input("Days until an invoice is due", min_value=0, step=1, value=int(settings["due_days"]))
        low = c3.number_input("Low-stock warning at", min_value=0, step=1, value=int(settings["low_stock_at"]))
        adv_default = st.number_input("Default advance payment for services (%)", min_value=1.0, max_value=100.0, step=5.0, value=float(settings["advance_pct"]))
        note = st.text_input("Default note on documents", settings["default_note"])

    with st.container(border=True):
        st.markdown('<div class="ob-h">Logo and signature</div>', unsafe_allow_html=True)
        st.caption("PNG files. The logo replaces the company name at the top of the PDF.")
        c1, c2 = st.columns(2)
        for col, label, path in ((c1, "Logo", LOGO_FILE), (c2, "Signature", SIGNATURE_FILE)):
            with col:
                up = st.file_uploader(label, type=["png"], key=f"up_{label}")
                if up is not None:
                    try:
                        Image.open(up).verify()
                        path.write_bytes(up.getvalue())
                        flash("success", f"{label} updated.")
                        st.rerun()
                    except Exception:
                        st.error("That file is not a valid PNG image.")
                if path.exists():
                    st.image(str(path), width=180)
                    if st.button(f"Remove {label.lower()}", key=f"rm_{label}"):
                        path.unlink()
                        st.rerun()

    if st.button("Save settings", type="primary"):
        save_settings({**settings, "company_name": name, "company_addr1": a1, "company_addr2": a2,
                       "phones": [str(x).strip() for x in phones_df["Phone number"].dropna() if str(x).strip()],
                       "email": email,
                       "bank_name": bank, "acc_num": acc, "acc_name": acc_name,
                       "vat_rate": vat, "due_days": int(due), "low_stock_at": int(low), "advance_pct": adv_default, "default_note": note})
        flash("success", "Settings saved.")
        st.rerun()


# ---------- main ----------
def main() -> None:
    st.set_page_config(page_title="Omohtech Billing", page_icon="🧾", layout="wide",
                       initial_sidebar_state="collapsed")
    require_login()
    settings = load_settings()
    init_state(settings)
    apply_pending(settings)

    accent = accent_for(st.session_state.doc_type)
    inject_css(accent, "#F3F7FD")

    hist = get_history()
    masthead(settings, hist, get_inventory())

    page = st.segmented_control("Page", PAGES, key="nav", label_visibility="collapsed") or PAGES[0]
    show_flash()
    if page == "New document":
        page_new_document(settings)
    elif page == "History":
        page_history(settings, hist)
    elif page == "Catalogue":
        page_catalogue(settings)
    else:
        page_settings(settings)


main()
