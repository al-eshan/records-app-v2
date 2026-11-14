# -*- coding: utf-8 -*-
import os
import math
import json
import hmac
import hashlib
import mimetypes
import sqlite3
import tempfile
import uuid
import urllib.request
import urllib.error
import urllib.parse
import time
import re

from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (
    Response, stream_with_context,
    Flask, g, render_template,
    request, redirect, url_for, session, flash,
    send_file, send_from_directory
)

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from openpyxl import Workbook

# =========================
# App Config / Constants
# =========================
APP_TITLE = "آل عِشان"
BASE_DIR = os.path.dirname(__file__)

# Database path:
# - Render / Production  → /var/data/data.db
# - Local development    → ./data.db
DATABASE = os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "data.db")

# =========================
# Home Slider / Uploads
# =========================
# Production (Render): if DATABASE_PATH=/var/data/data.db, slides go to /var/data/home_slides
# Local: slides go to ./home_slides beside data.db
SLIDES_DIR = os.path.join(os.path.dirname(DATABASE), "home_slides")
os.makedirs(SLIDES_DIR, exist_ok=True)

# WhatsApp media cache / temporary outbound files
WA_MEDIA_DIR = os.path.join(os.path.dirname(DATABASE), "wa_media")
os.makedirs(WA_MEDIA_DIR, exist_ok=True)

ALLOWED_SLIDE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def allowed_slide_file(filename):
    return (
        "." in (filename or "")
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_SLIDE_EXTENSIONS
    )

# المستخدم الرئيسي (فقط هذا يدخل صفحة الصلاحيات)
MASTER_USERNAME = "adm-es"

# صلاحيات النظام
PERM_KEYS = [
    "home",
    "accounting_home",
    "accounting_es1",
    "accounting_es2",
    "accounting_es3",
    "monthly_commission",
    "financial_commitments",
   "debts_es1",
   "debts_es2",
   "debts_es3",
    "records_home",
    "records_es1",
    "records_es2",
    "records_es3",
    "employees",
    "tasks",
    "suggestions",
    "accounting_analytics",
   "important_links",
    "home_slides",
    "dashboard",
    "accounting_edit_after_48h",
    "financial_admin_approval",
    "accounting_rejection_alerts",
    "whatsapp_branches",
    "whatsapp_numbers_manage",
    "permissions",
    "database_export",
]

PERM_LABELS = {
    "home": "الرئيسية",
    "accounting_home": "المحاسبة المالية ",
    "accounting_es1": "محاسبة يومية ES1",
    "accounting_es2": "محاسبة يومية ES2",
    "accounting_es3": "محاسبة يومية ES3",
    "monthly_commission": "عمولة المبيعات",
    "financial_commitments": "الالتزامات المالية",
    "debts_es1": "دفتر الديون - صلاحية ES1",
    "debts_es2": "دفتر الديون - صلاحية ES2",
    "debts_es3": "دفتر الديون - صلاحية ES3",
    "records_home": "السجلات",
    "records_es1": "سجلات ES1",
    "records_es2": "سجلات ES2",
    "records_es3": "سجلات ES3",
    "employees": "الموظفين",
    "tasks": "المهام",
    "suggestions": "الاقتراحات",
    "accounting_analytics": "التحليلات المحاسبية",
    "important_links": "روابط مهمة",
    "home_slides": "إدارة صور الرئيسية",
    "dashboard": "لوحة المتابعة",
    "accounting_edit_after_48h": "تعديل المحاسبة اليومية بعد 48 ساعة",
    "financial_admin_approval": "اعتماد الإدارة المالية",
    "accounting_rejection_alerts": "عرض تنبيهات المحاسبة المرفوضة",
    "whatsapp_branches": "واتساب الفروع",
    "whatsapp_numbers_manage": "إدارة أرقام واتساب",
    "permissions": "الصلاحيات",
    "database_export": "تصدير قاعدة البيانات",
}

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-this-secret")
# Cookie policy: Render serves HTTPS; keep localhost usable over HTTP.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (os.environ.get("COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on"))
# حد الرفع للطلب الواحد: 50 MB
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# =========================
# DB helpers (SQLite مؤقتًا)
# =========================
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        g._db = db
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    """Run schema.sql once manually (first run) OR call from __main__ (محلي فقط)."""
    db = sqlite3.connect(DATABASE)
    schema_path = os.path.join(BASE_DIR, "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def _table_exists(db, name: str) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False

def ensure_db_schema_once():
    """Ensure schema.sql has been applied (safe to call multiple times)."""
    if app.config.get("DB_BOOTSTRAPPED"):
        return

    db_dir = os.path.dirname(DATABASE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    db = get_db()

    if not _table_exists(db, "users"):
        schema_path = os.path.join(BASE_DIR, "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as f:
            db.executescript(f.read())
        db.commit()

    app.config["DB_BOOTSTRAPPED"] = True

def ensure_financial_commitments_schema(db):
    """Ensure financial_commitments table exists and has task_id column."""
    db.execute("""
    CREATE TABLE IF NOT EXISTS financial_commitments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        party TEXT NOT NULL,
        amount REAL NOT NULL DEFAULT 0,
        task_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(financial_commitments)").fetchall()]

    if "task_id" not in cols:
        db.execute("ALTER TABLE financial_commitments ADD COLUMN task_id INTEGER")

    db.commit()


def ensure_debts_ledger_schema(db):
    db.execute("""
    CREATE TABLE IF NOT EXISTS debts_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        es TEXT NOT NULL,
        invoice_date TEXT,
        customer_name TEXT,
        mobile TEXT,
        address TEXT,
        invoice_total REAL DEFAULT 0,
        paid_amount REAL DEFAULT 0,
        remaining_amount REAL DEFAULT 0,
        last_sent_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT
    )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(debts_ledger)").fetchall()]

    if "invoice_no" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN invoice_no TEXT")
    if "last_sent_at" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN last_sent_at TEXT")
    if "es" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN es TEXT NOT NULL DEFAULT 'ES1'")
    if "invoice_date" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN invoice_date TEXT")
    if "customer_name" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
    if "mobile" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN mobile TEXT")
    if "address" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN address TEXT")
    if "invoice_total" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN invoice_total REAL NOT NULL DEFAULT 0")
    if "paid_amount" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0")
    if "remaining_amount" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN remaining_amount REAL NOT NULL DEFAULT 0")
    if "created_at" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN created_at TEXT")
    if "updated_at" not in cols:
        db.execute("ALTER TABLE debts_ledger ADD COLUMN updated_at TEXT")

    db.commit()


def ensure_debt_deleted_archive(db):
    """Keep a reversible, immutable snapshot of each removed debt invoice."""
    db.execute("""CREATE TABLE IF NOT EXISTS debt_deleted_archive (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      debt_id INTEGER NOT NULL,
      branch_es TEXT NOT NULL,
      customer_id INTEGER,
      snapshot_json TEXT NOT NULL,
      deleted_by TEXT NOT NULL,
      deleted_at TEXT NOT NULL DEFAULT (datetime('now')),
      restored_by TEXT,
      restored_at TEXT
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_debt_deleted_archive_active
                  ON debt_deleted_archive(restored_at,deleted_at)""")
    db.commit()


def debt_delete_token():
    """CSRF protection for administrator-only debt deletion and restoration."""
    if session.get("username") != MASTER_USERNAME:
        return ""
    if not session.get("_debt_delete_token"):
        session["_debt_delete_token"] = uuid.uuid4().hex
    return session["_debt_delete_token"]


def debt_delete_token_valid():
    expected = session.get("_debt_delete_token") or ""
    provided = request.form.get("debt_delete_token") or ""
    return bool(expected and hmac.compare_digest(expected, provided))


def ensure_important_links_schema(db):
    db.execute("""
    CREATE TABLE IF NOT EXISTS important_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        category TEXT DEFAULT '',
        is_pinned INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(important_links)").fetchall()]

    if "category" not in cols:
        db.execute("ALTER TABLE important_links ADD COLUMN category TEXT DEFAULT ''")
    if "is_pinned" not in cols:
        db.execute("ALTER TABLE important_links ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
    if "sort_order" not in cols:
        db.execute("ALTER TABLE important_links ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in cols:
        db.execute("ALTER TABLE important_links ADD COLUMN created_at TEXT")
    if "updated_at" not in cols:
        db.execute("ALTER TABLE important_links ADD COLUMN updated_at TEXT")

    db.commit()


def ensure_home_slides_schema(db):
    """Ensure the home slider table exists and can be upgraded safely."""
    db.execute("""
    CREATE TABLE IF NOT EXISTS home_slides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        original_name TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(home_slides)").fetchall()]

    if "original_name" not in cols:
        db.execute("ALTER TABLE home_slides ADD COLUMN original_name TEXT")
    if "sort_order" not in cols:
        db.execute("ALTER TABLE home_slides ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    if "is_active" not in cols:
        db.execute("ALTER TABLE home_slides ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in cols:
        db.execute("ALTER TABLE home_slides ADD COLUMN created_at TEXT")

    db.commit()


def ensure_master_user():
    """Ensure master user exists and has ALL perms = 1."""
    db = get_db()

    row = db.execute("SELECT id FROM users WHERE username=?", (MASTER_USERNAME,)).fetchone()
    if row:
        uid = row["id"]
    else:
        default_pass = os.environ.get("MASTER_PASS", "1234")
        db.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
            (MASTER_USERNAME, generate_password_hash(default_pass), datetime.now().isoformat()),
        )
        db.commit()
        uid = db.execute(
            "SELECT id FROM users WHERE username=?",
            (MASTER_USERNAME,),
        ).fetchone()["id"]

    for k in PERM_KEYS:
        db.execute(
            """
            INSERT INTO permissions (user_id, perm_key, allowed)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, perm_key) DO UPDATE SET allowed=1
            """,
            (uid, k),
        )

    db.commit()


@app.before_request
def _ensure_bootstrap():
    try:
        ensure_db_schema_once()
        db = get_db()

        ensure_financial_commitments_schema(db)
        ensure_debts_ledger_schema(db)
        ensure_important_links_schema(db)
        ensure_home_slides_schema(db)
        ensure_whatsapp_schema(db)

        ensure_master_user()

    except Exception as e:
        print("bootstrap error:", e)
        pass

# =========================
# Financial Commitments -> Tasks helpers
# =========================
def _commitment_task_title(party, amount):
    return f"التزام مالي — {party} — {amount:.2f} ر.س"


def create_or_update_commitment_task(db, commitment_id):
    row = db.execute(
        "SELECT party, amount, task_id FROM financial_commitments WHERE id=?",
        (commitment_id,),
    ).fetchone()
    if not row:
        return

    title = _commitment_task_title(row["party"], float(row["amount"] or 0))

    if row["task_id"]:
        db.execute("UPDATE tasks SET title=? WHERE id=?", (title, row["task_id"]))
        db.commit()
        return

    cur = db.execute(
        "INSERT INTO tasks (title, due_date, created_at) VALUES (?, ?, ?)",
        (title, None, datetime.now().isoformat()),
    )
    task_id = cur.lastrowid

    db.execute(
        "UPDATE financial_commitments SET task_id=? WHERE id=?",
        (task_id, commitment_id),
    )
    db.commit()


def delete_commitment_task(db, commitment_id):
    row = db.execute(
        "SELECT task_id FROM financial_commitments WHERE id=?",
        (commitment_id,),
    ).fetchone()

    if row and row["task_id"]:
        db.execute("DELETE FROM tasks WHERE id=?", (row["task_id"],))
        db.commit()


# =========================
# Google Sheets (Apps Script Web App)
# =========================
# المتغيرات المطلوبة في Render/Environment:
#   GOOGLE_APPS_SCRIPT_URL = رابط Web App (Deploy as Web App) وينتهي /exec
#   GOOGLE_SHEETS_TOKEN    = توكن حماية بسيط (لا تشاركه)
#
# ملاحظة: كل التبويبات فيها id مخفي، بنعتمد upsert/delete على العمود A دائماً.

def _gs_enabled() -> bool:
    url = (os.environ.get("GOOGLE_APPS_SCRIPT_URL") or "").strip()
    token = (os.environ.get("GOOGLE_SHEETS_TOKEN") or "").strip()
    return bool(url) and bool(token)


def _gs_post(payload: dict):
    """
    يرسل POST إلى Apps Script Web App.
    يرجع: (data, err)
      data = dict (إن نجح)
      err  = str  (إن فشل)
    """
    if not _gs_enabled():
        print("GS: not configured (missing GOOGLE_APPS_SCRIPT_URL or GOOGLE_SHEETS_TOKEN)")
        return None, "gs_not_configured"

    url = (os.environ.get("GOOGLE_APPS_SCRIPT_URL") or "").strip()
    token = (os.environ.get("GOOGLE_SHEETS_TOKEN") or "").strip()

    payload = dict(payload or {})
    payload["token"] = token

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=25) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", errors="ignore").strip()

        # Logging واضح في Render
        print("GS POST ->", url, "status=", status, "resp=", raw[:500])

        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            return None, f"invalid_json_response:{raw[:200]}"

        if isinstance(data, dict) and data.get("ok") is False:
            return None, data.get("error") or "gs_error"

        return data, None

    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", errors="ignore")
        except Exception:
            msg = str(e)
        print("GS HTTPError", e.code, msg[:700])
        return None, f"http_error_{e.code}:{msg}"

    except urllib.error.URLError as e:
        print("GS URLError", str(e))
        return None, f"url_error:{e}"

    except Exception as e:
        print("GS request_failed", str(e))
        return None, f"request_failed:{e}"


def gs_post(action: str, tab: str, headers=None, row=None, row_id=None, rows=None):
    """
    Wrapper متوافق مع Apps Script الحالي:

    Actions:
      - ping
      - ensure_headers   (headers)
      - append           (row=list)
      - upsert           (row=list + row_id) [keyColumn=1, keyValue=id]
      - delete_by_id     (row_id) -> delete_by_key
    """
    if not tab:
        return None, "missing_tab"

    action = (action or "").strip()

    if action == "ping":
        return _gs_post({"action": "ping"})

    if action == "ensure_headers":
        payload = {
            "action": "ensure_headers",
            "sheet": tab,
            "headers": [str(h).strip() for h in (headers or [])],
        }
        return _gs_post(payload)

    if action == "append":
        if not isinstance(row, list):
            return None, "row_must_be_list"
        payload = {
            "action": "append",
            "sheet": tab,
            "row": ["" if v is None else str(v) for v in row],
        }
        return _gs_post(payload)

    if action == "upsert":
        if not isinstance(row, dict):
            return None, "row_must_be_dict"
        if not headers:
            return None, "missing_headers"
        if row_id is None:
            return None, "missing_row_id"

        headers_clean = [str(h).strip() for h in headers]
        if not headers_clean or headers_clean[0].lower() != "id":
            return None, "headers_must_start_with_id"

        ordered = [row.get(h, "") for h in headers_clean]

        payload = {
            "action": "upsert",
            "sheet": tab,
            "keyColumn": 1,
            "keyValue": str(row_id),
            "row": ["" if v is None else str(v) for v in ordered],
        }
        return _gs_post(payload)

    if action == "delete_by_id":
        if row_id is None:
            return None, "missing_row_id"
        payload = {
            "action": "delete_by_key",
            "sheet": tab,
            "keyColumn": 1,
            "keyValue": str(row_id),
        }
        return _gs_post(payload)
    if action == "replace_all":
        if not headers:
            return None, "missing_headers"
        if not isinstance(rows, list):
            return None, "rows_must_be_list"

        payload = {
            "action": "replace_all",
            "sheet": tab,
            "headers": [str(h).strip() for h in (headers or [])],
            "rows": rows,
        }
        return _gs_post(payload)

    return None, f"unknown_action:{action}"


# =========
# دوال توافق قديمة (حتى ما تغيّر كل المشروع)
# =========
def open_ws(tab_name: str):
    if not _gs_enabled():
        return None, "gs_not_configured"
    if not tab_name:
        return None, "missing_tab_name"
    return tab_name, None


def ws_ensure_headers(ws, headers):
    if not ws:
        return False
    headers = [str(h).strip() for h in (headers or []) if str(h).strip()]
    if not headers:
        return False
    _, err = gs_post("ensure_headers", tab=str(ws), headers=headers)
    if err:
        print("ws_ensure_headers error:", err)
        return False
    return True


def ws_upsert(ws, headers, row_values, row_id):
    if not ws:
        return False

    headers_clean = [str(h).strip() for h in (headers or [])]
    if not headers_clean or headers_clean[0].lower() != "id":
        print("ws_upsert error: headers must start with 'id'")
        return False

    if not ws_ensure_headers(ws, headers_clean):
        return False

    row_values = list(row_values or [])
    if len(row_values) != len(headers_clean):
        print("ws_upsert error: row_values length != headers length")
        return False

    payload = {
        "action": "upsert",
        "sheet": str(ws),
        "keyColumn": 1,
        "keyValue": str(row_id),
        "row": ["" if v is None else str(v) for v in row_values],
    }
    _, err = _gs_post(payload)
    if err:
        print("ws_upsert error:", err)
        return False
    return True


def ws_delete_by_id(ws, row_id):
    if not ws:
        return False
    data, err = gs_post("delete_by_id", tab=str(ws), row_id=row_id)
    if err:
        print("ws_delete_by_id error:", err)
        return False

    # يدعم أكثر من شكل للرد
    if isinstance(data, dict):
        if data.get("deleted") is True:
            return True
        if data.get("ok") is True and data.get("deleted") is None:
            # بعض السكربتات ترجع ok فقط
            return True

    return False

def ws_replace_all(tab_name: str, headers: list, rows: list):
    data, err = gs_post("replace_all", tab=tab_name, headers=headers, rows=rows)
    if err:
        print("ws_replace_all error:", err)
        return False
    return True


# =========================
# WhatsApp branches / unified inbox
# =========================
def ensure_whatsapp_schema(db):
    """Create/upgrade local WhatsApp inbox tables."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS wa_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            branch_code TEXT,
            phone_number TEXT,
            meta_phone_number_id TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS wa_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_number_id INTEGER NOT NULL,
            contact_wa_id TEXT,
            contact_name TEXT,
            last_message TEXT,
            last_message_at TEXT,
            last_inbound_at TEXT,
            unread_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            FOREIGN KEY (wa_number_id) REFERENCES wa_numbers(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS wa_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            meta_message_id TEXT,
            direction TEXT NOT NULL DEFAULT 'in',
            message_type TEXT NOT NULL DEFAULT 'text',
            body TEXT,
            media_url TEXT,
            meta_media_id TEXT,
            media_mime_type TEXT,
            media_filename TEXT,
            sent_at TEXT,
            status TEXT,
            status_error TEXT,
            sent_by_user_id INTEGER,
            sent_by_username TEXT,
            created_at TEXT,
            FOREIGN KEY (conversation_id) REFERENCES wa_conversations(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS wa_number_permissions (
            user_id INTEGER NOT NULL,
            wa_number_id INTEGER NOT NULL,
            can_view INTEGER NOT NULL DEFAULT 0,
            can_reply INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (user_id, wa_number_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (wa_number_id) REFERENCES wa_numbers(id)
        )
    """)

    # Safe migrations for existing databases.
    number_cols = [r[1] for r in db.execute("PRAGMA table_info(wa_numbers)").fetchall()]
    if "meta_phone_number_id" not in number_cols:
        db.execute("ALTER TABLE wa_numbers ADD COLUMN meta_phone_number_id TEXT")
    # Optional provider metadata: not credentials; safe, idempotent migrations.
    # Actual API keys remain environment variables in Render.
    if "gupshup_app_id" not in number_cols:
        db.execute("ALTER TABLE wa_numbers ADD COLUMN gupshup_app_id TEXT")
    if "gupshup_app_name" not in number_cols:
        db.execute("ALTER TABLE wa_numbers ADD COLUMN gupshup_app_name TEXT")

    conv_cols = [r[1] for r in db.execute("PRAGMA table_info(wa_conversations)").fetchall()]
    if "last_inbound_at" not in conv_cols:
        db.execute("ALTER TABLE wa_conversations ADD COLUMN last_inbound_at TEXT")
    # Administrative delete is reversible and preserves audit evidence.
    for col, data_type in (
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("deleted_by", "TEXT"),
    ):
        if col not in conv_cols:
            db.execute(f"ALTER TABLE wa_conversations ADD COLUMN {col} {data_type}")

    message_cols = [r[1] for r in db.execute("PRAGMA table_info(wa_messages)").fetchall()]
    migrations = {
        "meta_media_id": "TEXT",
        "media_mime_type": "TEXT",
        "media_filename": "TEXT",
        "status_error": "TEXT",
        "sent_by_user_id": "INTEGER",
        "sent_by_username": "TEXT",
    }
    for col, col_type in migrations.items():
        if col not in message_cols:
            db.execute(f"ALTER TABLE wa_messages ADD COLUMN {col} {col_type}")

    db.execute("CREATE INDEX IF NOT EXISTS idx_wa_conversations_number ON wa_conversations(wa_number_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_wa_conversations_contact ON wa_conversations(wa_number_id, contact_wa_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_conversation ON wa_messages(conversation_id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_messages_meta_id ON wa_messages(meta_message_id) WHERE meta_message_id IS NOT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS idx_wa_number_permissions_user ON wa_number_permissions(user_id)")

    count = db.execute("SELECT COUNT(*) AS c FROM wa_numbers").fetchone()["c"]
    if int(count or 0) == 0:
        now = datetime.now().isoformat(timespec="seconds")
        defaults = [
            ("ES1", "ES1", 1),
            ("ES2", "ES2", 2),
            ("ES3", "ES3", 3),
            ("الإدارة", "ADMIN", 4),
            ("الرقم الخامس", "WA5", 5),
        ]
        db.executemany(
            """
            INSERT INTO wa_numbers (label, branch_code, sort_order, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(label, code, order_no, now) for label, code, order_no in defaults],
        )

    db.commit()


def _wa_allowed_number_ids(db, require_reply=False):
    """Return active WhatsApp number IDs visible/repliable by the logged-in user.

    Backward compatibility: users who have the general WhatsApp permission but no
    per-number rows yet can see/reply to all active numbers. Once an admin saves
    per-number permissions for that user, those rows become authoritative.
    """
    if not session.get("logged_in"):
        return []

    active_rows = db.execute(
        "SELECT id FROM wa_numbers WHERE is_active=1 ORDER BY sort_order, id"
    ).fetchall()
    active_ids = [int(r["id"]) for r in active_rows]

    if session.get("username") == MASTER_USERNAME:
        return active_ids

    perms = session.get("perms") or {}
    if not bool(perms.get("whatsapp_branches")):
        return []

    user_id = session.get("user_id")
    if not user_id:
        return []

    rows = db.execute(
        """
        SELECT wa_number_id, can_view, can_reply
        FROM wa_number_permissions
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchall()

    if not rows:
        return active_ids

    allowed = []
    active_set = set(active_ids)
    for r in rows:
        nid = int(r["wa_number_id"])
        if nid not in active_set:
            continue
        ok = bool(r["can_reply"]) if require_reply else bool(r["can_view"] or r["can_reply"])
        if ok:
            allowed.append(nid)
    return allowed


def _wa_total_unread_for_session():
    if not session.get("logged_in"):
        return 0
    try:
        db = get_db()
        ensure_whatsapp_schema(db)
        ids = _wa_allowed_number_ids(db, require_reply=False)
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        row = db.execute(
            f"SELECT COALESCE(SUM(unread_count),0) AS c FROM wa_conversations WHERE wa_number_id IN ({placeholders}) AND is_deleted=0",
            ids,
        ).fetchone()
        return int(row["c"] or 0)
    except Exception as exc:
        print("WhatsApp unread context error:", exc)
        return 0

# =========================
# Debug routes (مؤقتة للتشخيص)
# =========================
@app.route("/debug/env", methods=["GET"])
def debug_env():
    # Diagnostics expose environment details: administration only.
    if not session.get("logged_in") or session.get("username") != MASTER_USERNAME:
        return "Forbidden", 403
    return {
        "DATABASE": DATABASE,
        "GS_ENABLED": _gs_enabled(),
        "GOOGLE_APPS_SCRIPT_URL_set": bool((os.environ.get("GOOGLE_APPS_SCRIPT_URL") or "").strip()),
        "GOOGLE_SHEETS_TOKEN_set": bool((os.environ.get("GOOGLE_SHEETS_TOKEN") or "").strip()),
    }


@app.route("/debug/gs", methods=["GET"])
def debug_gs():
    # Do not allow anonymous callers to invoke external Google Apps Script.
    if not session.get("logged_in") or session.get("username") != MASTER_USERNAME:
        return "Forbidden", 403
    # Ping فقط للتأكد أن Apps Script يرد JSON
    data, err = gs_post("ping", tab="any")
    return {"enabled": _gs_enabled(), "data": data, "err": err}





# =========================
# Auth + permissions
# =========================
def load_user_perms(user_id: int) -> dict:
    db = get_db()
    rows = db.execute(
        "SELECT perm_key, allowed FROM permissions WHERE user_id=?",
        (user_id,),
    ).fetchall()

    perms = {k: False for k in PERM_KEYS}
    for r in rows:
        perms[r["perm_key"]] = bool(r["allowed"])
    return perms


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def permission_required(perm_key: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            perms = session.get("perms") or {}
            if not bool(perms.get(perm_key)):
                return render_template("no_permission.html"), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def admin_required(fn):
    """Only MASTER_USERNAME can access."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("username") != MASTER_USERNAME:
            return render_template("no_permission.html"), 403
        perms = session.get("perms") or {}
        if not bool(perms.get("permissions")):
            return render_template("no_permission.html"), 403
        return fn(*args, **kwargs)
    return wrapper


# =========================
# Helpers for UI coloring
# =========================
def parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_left(expiry):
    if expiry is None:
        return None
    return (expiry - date.today()).days


def status_color(expiry):
    dl = days_left(expiry)
    if dl is None:
        return "bg-blue"
    if dl > 45:
        return "bg-green"
    if 1 <= dl <= 45:
        return "bg-yellow"
    return "bg-red"



def _allowed_alert_kinds(perms=None):
    """Accounting branches whose rejected alerts the current user may see."""
    if session.get("username") == MASTER_USERNAME:
        return ["ES1", "ES2", "ES3"]

    perms = perms or (session.get("perms") or {})
    if not bool(perms.get("accounting_rejection_alerts")):
        return []

    return [
        kind for kind in ("ES1", "ES2", "ES3")
        if bool(perms.get(f"accounting_{kind.lower()}"))
    ]


def _accounting_alert_rows(limit=None):
    """Return currently rejected accounting days visible to this user."""
    if not session.get("logged_in"):
        return []

    perms = session.get("perms") or {}
    if session.get("username") != MASTER_USERNAME and not bool(perms.get("accounting_rejection_alerts")):
        return []

    kinds = _allowed_alert_kinds(perms)
    if not kinds:
        return []

    try:
        db = get_db()
        ensure_daily_accounting_schema(db)

        placeholders = ",".join(["?"] * len(kinds))
        sql = f"""
            SELECT id,
                   es,
                   day_date,
                   financial_approval_status,
                   financial_approval_updated_at,
                   financial_approval_by
              FROM daily_header
             WHERE financial_approval_status='rejected'
               AND es IN ({placeholders})
             ORDER BY COALESCE(financial_approval_updated_at, day_date) DESC,
                      day_date DESC,
                      es ASC
        """
        params = list(kinds)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = db.execute(sql, params).fetchall()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        result = []

        for r in rows:
            updated_at = (r["financial_approval_updated_at"] or "").strip()
            age_hours = None
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                    age_hours = max(0, int((now_utc - dt).total_seconds() // 3600))
                except Exception:
                    age_hours = None

            result.append({
                "id": r["id"],
                "es": r["es"],
                "day_date": r["day_date"],
                "financial_approval_updated_at": updated_at,
                "financial_approval_by": (r["financial_approval_by"] or "").strip(),
                "age_hours": age_hours,
            })

        return result
    except Exception as exc:
        print("accounting alerts error:", exc)
        return []


def _accounting_alert_context():
    rows = _accounting_alert_rows()
    return {
        "accounting_alert_count": len(rows),
    }


def _can_export_database():
    """Use current DB permissions, not potentially stale session permissions."""
    if not session.get("logged_in") or not session.get("user_id"):
        return False
    if session.get("username") == MASTER_USERNAME:
        return True
    try:
        row = get_db().execute(
            "SELECT allowed FROM permissions WHERE user_id=? AND perm_key=?",
            (session["user_id"], "database_export"),
        ).fetchone()
        return bool(row and row["allowed"])
    except sqlite3.Error:
        return False


@app.context_processor
def inject_helpers():
    return {
        "APP_TITLE": APP_TITLE,
        "can_export_database": _can_export_database(),
        "days_left": days_left,
        "status_color": status_color,
        "perm_labels": PERM_LABELS,
        "perm_keys": PERM_KEYS,
        "whatsapp_unread_count": _wa_total_unread_for_session(),
        **_accounting_alert_context(),
    }


# =========================
# Basic routes
# =========================
@app.route("/", methods=["GET"])
def root():
    if session.get("logged_in"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()

        db = get_db()
        user = db.execute(
            "SELECT id, username, password_hash, is_admin FROM users WHERE username=?",
            (u,),
        ).fetchone()

        if user and check_password_hash(user["password_hash"], p):
            session.clear()
            session["logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = int(user["is_admin"] or 0) == 1
            session["perms"] = load_user_perms(user["id"])
            return redirect(url_for("home"))

        flash("اسم المستخدم أو كلمة المرور غير صحيحة", "danger")

    return render_template("login.html")


# =========================
# Database backup export (read-only online SQLite backup)
# =========================
@app.route("/database/export", methods=["GET"])
@login_required
def export_database():
    # Check fresh DB permission on every request, not the browser session.
    if not _can_export_database():
        return render_template("no_permission.html"), 403

    source_path = os.path.abspath(DATABASE)
    if not os.path.isfile(source_path):
        app.logger.error("Database export failed: source database is missing")
        return "تعذر العثور على قاعدة البيانات", 503

    backup_path = None
    try:
        # Temp file is separate from the live database. Never overwrite data.db.
        with tempfile.NamedTemporaryFile(prefix="eshan_export_", suffix=".db", delete=False) as f:
            backup_path = f.name

        # Opening the live database in read-only mode prevents accidental writes.
        # SQLite online backup keeps a transactionally consistent snapshot.
        source_uri = "file:" + urllib.parse.quote(source_path, safe="/") + "?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
            with sqlite3.connect(backup_path, timeout=30) as destination:
                source.backup(destination, pages=256, sleep=0.1)
                status = destination.execute("PRAGMA integrity_check").fetchone()
                if not status or status[0] != "ok":
                    raise sqlite3.DatabaseError("Backup failed integrity_check")

        size = os.path.getsize(backup_path)
        filename = "Al_Eshan_DB_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db"

        @stream_with_context
        def read_backup():
            try:
                with open(backup_path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.unlink(backup_path)
                except OSError:
                    app.logger.warning("Unable to remove a temporary database export")

        result = Response(read_backup(), mimetype="application/octet-stream")
        result.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        result.headers["Content-Length"] = str(size)
        result.headers["Cache-Control"] = "private, no-store, max-age=0"
        result.headers["Pragma"] = "no-cache"
        result.headers["X-Content-Type-Options"] = "nosniff"
        app.logger.info("Database export requested by user_id=%s", session.get("user_id"))
        return result
    except (sqlite3.Error, OSError) as exc:
        app.logger.exception("Database export failed: %s", type(exc).__name__)
        if backup_path and os.path.exists(backup_path):
            try:
                os.unlink(backup_path)
            except OSError:
                pass
        return "تعذر إنشاء النسخة الاحتياطية؛ لم يتم تعديل قاعدة البيانات الأصلية", 503


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/home")
@login_required
@permission_required("home")
def home():
    db = get_db()
    ensure_home_slides_schema(db)

    slides = db.execute("""
        SELECT id, filename, original_name, sort_order, is_active, created_at
        FROM home_slides
        WHERE is_active = 1
        ORDER BY sort_order ASC, id ASC
    """).fetchall()

    return render_template(
        "home.html",
        slides=slides
    )



# =========================
# WhatsApp Cloud API integration
# =========================
def _wa_webhook_verify_token():
    return (os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN") or "").strip()


def _wa_meta_app_secret():
    return (os.environ.get("WHATSAPP_META_APP_SECRET") or "").strip()


def _wa_access_token():
    return (os.environ.get("WHATSAPP_META_ACCESS_TOKEN") or "").strip()


def _wa_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _gupshup_webhook_secret():
    return (os.environ.get("GUPSHUP_WEBHOOK_SECRET") or "").strip()


def _gupshup_branch_suffix(branch_code=None):
    raw = str(branch_code or "").strip().upper()
    return "".join(ch if ch.isalnum() else "_" for ch in raw)


def _gupshup_env(name, branch_code=None):
    suffix = _gupshup_branch_suffix(branch_code)
    if suffix:
        value = (os.environ.get(f"{name}_{suffix}") or "").strip()
        if value:
            return value
    return (os.environ.get(name) or "").strip()


def _gupshup_api_key(branch_code=None):
    return _gupshup_env("GUPSHUP_API_KEY", branch_code)


def _gupshup_provider_setting(branch_code, field):
    """Read saved provider metadata for a branch; never store API secrets here."""
    if not branch_code or field not in ("gupshup_app_id", "gupshup_app_name"):
        return ""
    try:
        db = get_db()
        row = db.execute(
            f"SELECT {field} FROM wa_numbers WHERE branch_code=? AND is_active=1 "
            "ORDER BY sort_order, id LIMIT 1", (str(branch_code).strip(),)
        ).fetchone()
        return str(row[field] or "").strip() if row else ""
    except sqlite3.Error:
        # Old databases may not have the new columns until ensure_whatsapp_schema().
        return ""


def _gupshup_app_name(branch_code=None):
    return (_gupshup_provider_setting(branch_code, "gupshup_app_name")
            or _gupshup_env("GUPSHUP_APP_NAME", branch_code) or "AlEshan")


def _gupshup_source_number(branch_code=None, fallback=None):
    return _wa_digits(_gupshup_env("GUPSHUP_SOURCE_NUMBER", branch_code) or fallback)


def _gupshup_app_id(branch_code=None):
    return (_gupshup_provider_setting(branch_code, "gupshup_app_id")
            or _gupshup_env("GUPSHUP_APP_ID", branch_code))


def _gupshup_app_token(branch_code=None):
    return _gupshup_env("GUPSHUP_APP_TOKEN", branch_code)


def _gupshup_any_api_ready():
    if _gupshup_api_key():
        return True
    return any(k.startswith("GUPSHUP_API_KEY_") and str(v or "").strip()
               for k, v in os.environ.items())


def _wa_normalize_destination(value):
    """Normalize customer phone to international digits; Saudi local numbers are auto-expanded."""
    digits = _wa_digits(value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("05") and len(digits) == 10:
        digits = "966" + digits[1:]
    elif digits.startswith("5") and len(digits) == 9:
        digits = "966" + digits
    if not (8 <= len(digits) <= 15):
        return ""
    return digits


def _gupshup_extract_template_list(data):
    """Best-effort parser for Gupshup template-list response shapes."""
    if isinstance(data, list):
        if all(isinstance(x, dict) for x in data):
            return data
        return []
    if not isinstance(data, dict):
        return []
    preferred = ("templates", "templateList", "template_list", "data", "items", "results")
    for key in preferred:
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(x, dict) for x in value):
            return value
        if isinstance(value, dict):
            nested = _gupshup_extract_template_list(value)
            if nested:
                return nested
    for value in data.values():
        if isinstance(value, dict):
            nested = _gupshup_extract_template_list(value)
            if nested:
                return nested
    return []


def _gupshup_get_approved_text_templates(branch_code=None):
    api_key = _gupshup_api_key(branch_code)
    app_id = _gupshup_app_id(branch_code)
    if not api_key:
        return [], "لا يوجد Gupshup API Key لهذا الرقم. يجب إضافته في Render ضمن Environment."
    if not app_id:
        return [], "معرّف Gupshup App ID مفقود. ادخل إلى واتساب ← فحص الإرسال لضبطه لهذا الرقم."
    qs = urllib.parse.urlencode({
        "pageNo": 0,
        "pageSize": 100,
        "templateStatus": "APPROVED",
        "templateType": "TEXT",
    })
    url = f"https://api.gupshup.io/wa/app/{urllib.parse.quote(app_id)}/template?{qs}"
    req = urllib.request.Request(url, headers={"apikey": api_key, "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return [], f"تعذر جلب القوالب من Gupshup (HTTP {exc.code}): {raw[:180]}"
    except Exception as exc:
        return [], f"تعذر جلب القوالب من Gupshup: {exc}"

    rows = _gupshup_extract_template_list(data)
    out = []
    for t in rows:
        tid = str(t.get("id") or t.get("templateId") or t.get("template_id") or "").strip()
        if not tid:
            continue
        name = str(t.get("elementName") or t.get("templateName") or t.get("name") or tid).strip()
        language = str(t.get("languageCode") or t.get("language") or t.get("lang") or "").strip()
        category = str(t.get("category") or t.get("templateCategory") or "").strip()
        body = str(t.get("body") or t.get("templateBody") or t.get("content") or t.get("text") or "").strip()
        nums = [int(x) for x in re.findall(r"\{\{(\d+)\}\}", body)]
        param_count = max(nums) if nums else 0
        out.append({
            "id": tid, "name": name, "language": language, "category": category,
            "body": body, "param_count": param_count,
        })
    out.sort(key=lambda x: (x["name"].lower(), x["language"]))
    return out, None


def _gupshup_post_template(branch_code, source_number, destination, template_id, params=None):
    """Send an approved Gupshup template message to start/reopen a conversation."""
    api_key = _gupshup_api_key(branch_code)
    source = _gupshup_source_number(branch_code, source_number)
    app_name = _gupshup_app_name(branch_code)
    destination = _wa_normalize_destination(destination)
    if not api_key:
        return None, "GUPSHUP_API_KEY غير مضبوط في Render"
    if not source:
        return None, "رقم المصدر في Gupshup غير مضبوط"
    if not destination:
        return None, "رقم العميل غير صالح"
    form = {
        "channel": "whatsapp",
        "source": source,
        "destination": destination,
        "src.name": app_name,
        "template": json.dumps({"id": template_id, "params": list(params or [])}, ensure_ascii=False),
    }
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        "https://api.gupshup.io/wa/api/v1/template/msg",
        data=body,
        headers={"apikey": api_key, "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        if str(data.get("status") or "").lower() == "error":
            return None, str(data.get("message") or "Gupshup رفض القالب")
        mid = str(data.get("messageId") or data.get("message_id") or "").strip()
        return (mid or None), (None if mid else f"Gupshup لم يُرجع Message ID. response={raw[:250]}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            msg = data.get("message") or data.get("error") or raw
        except Exception:
            msg = raw
        if exc.code == 401:
            return None, "Gupshup HTTP 401: مفتاح API غير صالح لهذا التطبيق أو رقم الإرسال؛ راجع مفتاح Render ومصدر الرقم في Gupshup."
        return None, f"Gupshup HTTP {exc.code}: {msg}"
    except Exception as exc:
        return None, f"تعذر الاتصال بـ Gupshup: {exc}"


def _gupshup_post_message(branch_code, source_number, destination, message_obj):
    """Send a WhatsApp session message through Gupshup Self-Serve v1 API."""
    api_key = _gupshup_api_key(branch_code)
    source = _gupshup_source_number(branch_code, source_number)
    app_name = _gupshup_app_name(branch_code)
    destination = _wa_digits(destination)
    if not api_key:
        return None, f"GUPSHUP_API_KEY{('_' + _gupshup_branch_suffix(branch_code)) if branch_code else ''} غير مضبوط في Render"
    if not source:
        return None, "رقم المصدر في Gupshup غير مضبوط"
    if not destination:
        return None, "رقم العميل غير صالح"

    form = {
        "channel": "whatsapp",
        "source": source,
        "destination": destination,
        "src.name": app_name,
        "message": json.dumps(message_obj, ensure_ascii=False),
    }
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        "https://api.gupshup.io/wa/api/v1/msg",
        data=body,
        headers={
            "apikey": api_key,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        status = str(data.get("status") or "").lower().strip()
        if status == "error":
            return None, str(data.get("message") or "Gupshup رفض إرسال الرسالة")
        message_id = str(data.get("messageId") or data.get("message_id") or "").strip()
        print("Gupshup send response:", {"status": status, "messageId": message_id})
        if not message_id:
            return None, f"Gupshup لم يُرجع Message ID. response={raw[:300]}"
        return message_id, None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            msg = data.get("message") or data.get("error") or raw
        except Exception:
            msg = raw
        return None, f"Gupshup HTTP {exc.code}: {msg}"
    except Exception as exc:
        return None, f"تعذر الاتصال بـ Gupshup: {exc}"


def _wa_media_signature(filename, expires):
    secret = (app.secret_key or "change-this-secret").encode("utf-8")
    payload = f"{filename}:{int(expires)}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _wa_save_outbound_media(file_storage):
    original = secure_filename(file_storage.filename or "attachment") or "attachment"
    ext = os.path.splitext(original)[1].lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(WA_MEDIA_DIR, stored)
    file_storage.save(path)
    size = os.path.getsize(path)
    if size > 100 * 1024 * 1024:
        try:
            os.remove(path)
        except OSError:
            pass
        return None, None, None, "حجم المرفق أكبر من 100MB"
    mime = file_storage.mimetype or mimetypes.guess_type(original)[0] or "application/octet-stream"
    return stored, mime, original, None


def _wa_public_media_url(stored_name, ttl_seconds=3600):
    expires = int(time.time()) + int(ttl_seconds)
    sig = _wa_media_signature(stored_name, expires)
    return request.url_root.rstrip("/") + url_for(
        "whatsapp_public_media", filename=stored_name, exp=expires, sig=sig
    )


def _wa_gupshup_number(db, metadata):
    """Resolve a Gupshup v3 webhook to one of the configured branch numbers."""
    display_phone = _wa_digits((metadata or {}).get("display_phone_number"))
    phone_number_id = str((metadata or {}).get("phone_number_id") or "").strip()
    rows = db.execute(
        "SELECT id, label, branch_code, phone_number, meta_phone_number_id FROM wa_numbers WHERE is_active=1 ORDER BY sort_order, id"
    ).fetchall()
    for row in rows:
        if display_phone and _wa_digits(row["phone_number"]) == display_phone:
            if phone_number_id and (row["meta_phone_number_id"] or "") != phone_number_id:
                db.execute("UPDATE wa_numbers SET meta_phone_number_id=? WHERE id=?", (phone_number_id, row["id"]))
            return row
    if phone_number_id:
        for row in rows:
            if str(row["meta_phone_number_id"] or "").strip() == phone_number_id:
                return row

    # First-number fallback for initial deployment.
    env_source = _wa_digits(os.environ.get("GUPSHUP_SOURCE_NUMBER"))
    env_branch = (os.environ.get("GUPSHUP_BRANCH_CODE") or "").strip()
    if display_phone and env_source and display_phone == env_source and env_branch:
        row = db.execute(
            "SELECT id, label, branch_code, phone_number, meta_phone_number_id FROM wa_numbers WHERE branch_code=? AND is_active=1 LIMIT 1",
            (env_branch,),
        ).fetchone()
        if row:
            db.execute(
                "UPDATE wa_numbers SET phone_number=?, meta_phone_number_id=COALESCE(NULLIF(?,''), meta_phone_number_id) WHERE id=?",
                (display_phone, phone_number_id, row["id"]),
            )
            return db.execute(
                "SELECT id, label, branch_code, phone_number, meta_phone_number_id FROM wa_numbers WHERE id=?",
                (row["id"],),
            ).fetchone()
    return None


def _wa_graph_version():
    value = (os.environ.get("WHATSAPP_GRAPH_API_VERSION") or "v26.0").strip()
    if not value.startswith("v"):
        value = "v" + value
    return value


def _wa_signature_valid(raw_body: bytes, signature_header: str) -> bool:
    secret = _wa_meta_app_secret()
    if not secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _wa_graph_request(method, path, payload=None, raw_body=None, extra_headers=None, timeout=25):
    """Call Meta Graph API without exposing the token in templates/logs."""
    token = _wa_access_token()
    if not token:
        return None, "WHATSAPP_META_ACCESS_TOKEN غير مضبوط في Render"

    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = f"https://graph.facebook.com/{_wa_graph_version()}/{path.lstrip('/')}"

    headers = {"Authorization": f"Bearer {token}"}
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or (body[:1] in (b"{", b"[")):
                return json.loads(body.decode("utf-8")), None
            return {"raw": body, "content_type": ctype}, None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            message = ((parsed.get("error") or {}).get("message") or body).strip()
            code = (parsed.get("error") or {}).get("code")
            subcode = (parsed.get("error") or {}).get("error_subcode")
            suffix = ""
            if code is not None:
                suffix += f" (code {code}"
                if subcode is not None:
                    suffix += f", subcode {subcode}"
                suffix += ")"
            return None, message + suffix
        except Exception:
            return None, body or f"HTTP {exc.code}"
    except Exception as exc:
        return None, str(exc)


def _wa_upload_media(phone_number_id, file_storage):
    filename = secure_filename(file_storage.filename or "attachment") or "attachment"
    mime_type = file_storage.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    content = file_storage.read()
    max_bytes = 16 * 1024 * 1024
    if len(content) > max_bytes:
        return None, None, None, "حجم المرفق أكبر من 16MB؛ صغّر الملف ثم حاول مرة أخرى"

    boundary = "----AlEshanWhatsApp" + uuid.uuid4().hex
    parts = []
    def add_field(name, value):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    add_field("messaging_product", "whatsapp")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
    )
    parts.append(f"Content-Type: {mime_type}\r\n\r\n".encode())
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    data, err = _wa_graph_request(
        "POST",
        f"{phone_number_id}/media",
        raw_body=body,
        extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=45,
    )
    if err:
        return None, None, None, err
    media_id = str((data or {}).get("id") or "").strip()
    if not media_id:
        return None, None, None, "Meta لم تُرجع Media ID"
    return media_id, mime_type, filename, None


def _wa_send_payload(phone_number_id, payload):
    data, err = _wa_graph_request("POST", f"{phone_number_id}/messages", payload=payload)
    if err:
        return None, err
    messages = (data or {}).get("messages") or []
    if not messages:
        return None, "تم قبول الطلب لكن Meta لم تُرجع Message ID"
    message_id = str(messages[0].get("id") or "").strip()
    return message_id or None, None


def _wa_mark_read(phone_number_id, meta_message_id):
    if not phone_number_id or not meta_message_id or not _wa_access_token():
        return
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": meta_message_id,
    }
    _, err = _wa_graph_request("POST", f"{phone_number_id}/messages", payload=payload)
    if err:
        print("WhatsApp mark-read warning:", err)


def _wa_message_summary(message: dict):
    """Return body/media metadata for common WhatsApp message types."""
    msg_type = (message.get("type") or "unknown").strip()
    body = ""
    media_id = None
    mime_type = None
    filename = None
    media_url = None

    if msg_type == "text":
        body = ((message.get("text") or {}).get("body") or "").strip()
    elif msg_type in ("image", "video", "audio", "document", "sticker"):
        data = message.get(msg_type) or {}
        media_id = data.get("id") or data.get("mediaId")
        mime_type = data.get("mime_type") or data.get("content-type") or data.get("contentType")
        filename = data.get("filename") or data.get("name")
        media_url = data.get("url") or data.get("link") or data.get("originalUrl")
        caption = (data.get("caption") or "").strip()
        labels = {
            "image": "📷 صورة",
            "video": "🎥 فيديو",
            "audio": "🎵 رسالة صوتية",
            "document": "📄 مستند",
            "sticker": "🖼️ ملصق",
        }
        body = labels[msg_type]
        tail = filename or caption
        if tail:
            body += f": {tail}"
    elif msg_type == "location":
        data = message.get("location") or {}
        name = (data.get("name") or "").strip()
        address = (data.get("address") or "").strip()
        lat = data.get("latitude")
        lon = data.get("longitude")
        body = "📍 موقع"
        if name:
            body += f": {name}"
        elif address:
            body += f": {address}"
        elif lat is not None and lon is not None:
            body += f": {lat}, {lon}"
    elif msg_type == "button":
        data = message.get("button") or {}
        body = (data.get("text") or data.get("payload") or "🔘 زر").strip()
    elif msg_type == "interactive":
        data = message.get("interactive") or {}
        button_reply = data.get("button_reply") or {}
        list_reply = data.get("list_reply") or {}
        body = button_reply.get("title") or list_reply.get("title") or list_reply.get("description") or "↩️ رد تفاعلي"
    elif msg_type == "reaction":
        emoji = ((message.get("reaction") or {}).get("emoji") or "").strip()
        body = f"تفاعل {emoji}".strip()
    elif msg_type == "contacts":
        contacts = message.get("contacts") or []
        if contacts:
            name_data = contacts[0].get("name") or {}
            body = f"👤 جهة اتصال: {name_data.get('formatted_name') or ''}".strip()
        else:
            body = "👤 جهة اتصال"
    else:
        body = f"رسالة من نوع {msg_type}"

    return body or "رسالة واتساب", media_id, mime_type, filename, media_url


def _wa_iso_from_unix(timestamp_value):
    try:
        return datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _wa_parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _wa_freeform_open(last_inbound_at):
    dt = _wa_parse_iso(last_inbound_at)
    if not dt:
        return False, None
    expires = dt + timedelta(hours=24)
    return datetime.now(timezone.utc) < expires, expires


@app.route("/webhooks/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        mode = (request.args.get("hub.mode") or "").strip()
        token = (request.args.get("hub.verify_token") or "").strip()
        challenge = request.args.get("hub.challenge") or ""
        expected_token = _wa_webhook_verify_token()
        if expected_token and mode == "subscribe" and hmac.compare_digest(token, expected_token):
            return challenge, 200, {"Content-Type": "text/plain; charset=utf-8"}
        return "Forbidden", 403

    raw_body = request.get_data(cache=True)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _wa_meta_app_secret():
        print("WhatsApp webhook rejected: WHATSAPP_META_APP_SECRET is not configured")
        return "Webhook app secret is not configured", 503
    if not _wa_signature_valid(raw_body, signature):
        print("WhatsApp webhook rejected: invalid Meta signature")
        return "Invalid signature", 401

    payload = request.get_json(silent=True) or {}
    if payload.get("object") != "whatsapp_business_account":
        return "EVENT_RECEIVED", 200

    db = get_db()
    ensure_whatsapp_schema(db)
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                meta_phone_number_id = str(metadata.get("phone_number_id") or "").strip()

                for status_item in value.get("statuses") or []:
                    meta_message_id = str(status_item.get("id") or "").strip()
                    status_name = str(status_item.get("status") or "").strip()
                    errors = status_item.get("errors") or []
                    error_text = None
                    if errors:
                        e = errors[0] or {}
                        error_text = (e.get("title") or e.get("message") or "").strip() or None
                    if meta_message_id and status_name:
                        db.execute(
                            "UPDATE wa_messages SET status=?, status_error=? WHERE meta_message_id=?",
                            (status_name, error_text, meta_message_id),
                        )

                incoming_messages = value.get("messages") or []
                if not incoming_messages:
                    continue

                wa_number = db.execute(
                    """
                    SELECT id, label
                    FROM wa_numbers
                    WHERE meta_phone_number_id=? AND is_active=1
                    LIMIT 1
                    """,
                    (meta_phone_number_id,),
                ).fetchone()
                if not wa_number:
                    print("WhatsApp webhook: no active number mapped to Phone Number ID", meta_phone_number_id)
                    continue

                contact_names = {}
                for contact in value.get("contacts") or []:
                    wa_id = str(contact.get("wa_id") or "").strip()
                    profile = contact.get("profile") or {}
                    if wa_id:
                        contact_names[wa_id] = (profile.get("name") or "").strip()

                for message in incoming_messages:
                    meta_message_id = str(message.get("id") or "").strip()
                    if meta_message_id:
                        exists = db.execute(
                            "SELECT id FROM wa_messages WHERE meta_message_id=? LIMIT 1",
                            (meta_message_id,),
                        ).fetchone()
                        if exists:
                            continue

                    contact_wa_id = str(message.get("from") or "").strip()
                    if not contact_wa_id:
                        continue
                    contact_name = contact_names.get(contact_wa_id) or contact_wa_id
                    msg_type = str(message.get("type") or "unknown").strip()
                    body, meta_media_id, media_mime_type, media_filename, media_url = _wa_message_summary(message)
                    sent_at = _wa_iso_from_unix(message.get("timestamp"))
                    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

                    conversation = db.execute(
                        """
                        SELECT id FROM wa_conversations
                        WHERE wa_number_id=? AND contact_wa_id=?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (wa_number["id"], contact_wa_id),
                    ).fetchone()
                    if conversation:
                        conversation_id = conversation["id"]
                        db.execute(
                            """
                            UPDATE wa_conversations
                               SET contact_name=?, last_message=?, last_message_at=?,
                                   last_inbound_at=?, unread_count=COALESCE(unread_count,0)+1,
                                   updated_at=?, is_deleted=0, deleted_at=NULL, deleted_by=NULL
                             WHERE id=?
                            """,
                            (contact_name, body, sent_at, sent_at, now_iso, conversation_id),
                        )
                    else:
                        cur = db.execute(
                            """
                            INSERT INTO wa_conversations (
                                wa_number_id, contact_wa_id, contact_name, last_message,
                                last_message_at, last_inbound_at, unread_count, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (wa_number["id"], contact_wa_id, contact_name, body, sent_at, sent_at, now_iso),
                        )
                        conversation_id = cur.lastrowid

                    db.execute(
                        """
                        INSERT INTO wa_messages (
                            conversation_id, meta_message_id, direction, message_type,
                            body, media_url, meta_media_id, media_mime_type, media_filename,
                            sent_at, status, status_error, sent_by_user_id,
                            sent_by_username, created_at
                        ) VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, 'received', NULL, NULL, NULL, ?)
                        """,
                        (conversation_id, meta_message_id or None, msg_type, body, media_url,
                         meta_media_id, media_mime_type, media_filename, sent_at, now_iso),
                    )
        db.commit()
    except Exception as exc:
        db.rollback()
        print("WhatsApp webhook processing error:", repr(exc))
        return "Webhook processing failed", 500
    return "EVENT_RECEIVED", 200



@app.route("/webhooks/gupshup", methods=["POST"])
def gupshup_webhook():
    """Receive Gupshup Meta-format v3 events, including Coexistence echoes."""
    expected_secret = _gupshup_webhook_secret()
    if expected_secret:
        received_secret = (request.headers.get("X-Gupshup-Webhook-Secret") or "").strip()
        if not received_secret or not hmac.compare_digest(received_secret, expected_secret):
            print("Gupshup webhook rejected: invalid secret")
            return "", 401

    payload = request.get_json(silent=True) or {}
    # Gupshup sends a callback-set event when a subscription is created. Always acknowledge it.
    if not isinstance(payload, dict):
        return "", 200

    db = get_db()
    ensure_whatsapp_schema(db)
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                field = str(change.get("field") or "").strip()
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                wa_number = _wa_gupshup_number(db, metadata)

                # Delivery / read / failed / queued states.
                for status_item in value.get("statuses") or []:
                    status_name = str(status_item.get("status") or status_item.get("type") or "").strip()
                    candidate_ids = [
                        str(status_item.get("gs_id") or "").strip(),
                        str(status_item.get("id") or "").strip(),
                    ]
                    errors = status_item.get("errors") or []
                    error_text = None
                    if errors:
                        e = errors[0] or {}
                        error_text = (e.get("title") or e.get("message") or e.get("details") or "").strip() or None
                    if status_name:
                        for mid in [x for x in candidate_ids if x]:
                            db.execute(
                                "UPDATE wa_messages SET status=?, status_error=? WHERE meta_message_id=?",
                                (status_name, error_text, mid),
                            )

                if field == "messages":
                    incoming_messages = value.get("messages") or []
                    if incoming_messages and not wa_number:
                        print("Gupshup webhook: no branch number mapped for metadata", metadata)
                        continue

                    contact_names = {}
                    for contact in value.get("contacts") or []:
                        wa_id = _wa_digits(contact.get("wa_id") or contact.get("phone"))
                        profile = contact.get("profile") or {}
                        if wa_id:
                            contact_names[wa_id] = (profile.get("name") or "").strip()

                    for message in incoming_messages:
                        meta_message_id = str(message.get("id") or "").strip()
                        if meta_message_id:
                            exists = db.execute(
                                "SELECT id FROM wa_messages WHERE meta_message_id=? LIMIT 1",
                                (meta_message_id,),
                            ).fetchone()
                            if exists:
                                continue
                        contact_wa_id = _wa_digits(message.get("from"))
                        if not contact_wa_id:
                            continue
                        contact_name = contact_names.get(contact_wa_id) or contact_wa_id
                        msg_type = str(message.get("type") or "unknown").strip()
                        body, meta_media_id, media_mime_type, media_filename, media_url = _wa_message_summary(message)
                        sent_at = _wa_iso_from_unix(message.get("timestamp"))
                        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

                        conversation = db.execute(
                            "SELECT id FROM wa_conversations WHERE wa_number_id=? AND contact_wa_id=? ORDER BY id DESC LIMIT 1",
                            (wa_number["id"], contact_wa_id),
                        ).fetchone()
                        if conversation:
                            conversation_id = conversation["id"]
                            db.execute(
                                """
                                UPDATE wa_conversations
                                   SET contact_name=?, last_message=?, last_message_at=?, last_inbound_at=?,
                                       unread_count=COALESCE(unread_count,0)+1, updated_at=?,
                                   is_deleted=0, deleted_at=NULL, deleted_by=NULL
                                 WHERE id=?
                                """,
                                (contact_name, body, sent_at, sent_at, now_iso, conversation_id),
                            )
                        else:
                            cur = db.execute(
                                """
                                INSERT INTO wa_conversations
                                    (wa_number_id, contact_wa_id, contact_name, last_message,
                                     last_message_at, last_inbound_at, unread_count, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                                """,
                                (wa_number["id"], contact_wa_id, contact_name, body, sent_at, sent_at, now_iso),
                            )
                            conversation_id = cur.lastrowid

                        db.execute(
                            """
                            INSERT INTO wa_messages (
                                conversation_id, meta_message_id, direction, message_type, body,
                                media_url, meta_media_id, media_mime_type, media_filename,
                                sent_at, status, status_error, sent_by_user_id, sent_by_username, created_at
                            ) VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, 'received', NULL, NULL, NULL, ?)
                            """,
                            (conversation_id, meta_message_id or None, msg_type, body, media_url,
                             meta_media_id, media_mime_type, media_filename, sent_at, now_iso),
                        )

                elif field == "smb_message_echoes":
                    # Messages sent by staff from the WhatsApp Business mobile app.
                    echoes = value.get("message_echoes") or value.get("messages") or []
                    if echoes and not wa_number:
                        print("Gupshup coexistence webhook: no branch number mapped", metadata)
                        continue
                    contact_names = {}
                    for contact in value.get("contacts") or []:
                        wa_id = _wa_digits(contact.get("wa_id") or contact.get("phone"))
                        profile = contact.get("profile") or {}
                        if wa_id:
                            contact_names[wa_id] = (profile.get("name") or "").strip()
                    for message in echoes:
                        meta_message_id = str(message.get("id") or "").strip()
                        if meta_message_id:
                            exists = db.execute(
                                "SELECT id FROM wa_messages WHERE meta_message_id=? LIMIT 1",
                                (meta_message_id,),
                            ).fetchone()
                            if exists:
                                continue
                        contact_wa_id = _wa_digits(message.get("to") or message.get("recipient_id"))
                        if not contact_wa_id:
                            continue
                        contact_name = contact_names.get(contact_wa_id) or contact_wa_id
                        msg_type = str(message.get("type") or "unknown").strip()
                        body, meta_media_id, media_mime_type, media_filename, media_url = _wa_message_summary(message)
                        sent_at = _wa_iso_from_unix(message.get("timestamp"))
                        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

                        conversation = db.execute(
                            "SELECT id FROM wa_conversations WHERE wa_number_id=? AND contact_wa_id=? ORDER BY id DESC LIMIT 1",
                            (wa_number["id"], contact_wa_id),
                        ).fetchone()
                        if conversation:
                            conversation_id = conversation["id"]
                            db.execute(
                                "UPDATE wa_conversations SET contact_name=?, last_message=?, last_message_at=?, updated_at=? WHERE id=?",
                                (contact_name, body, sent_at, now_iso, conversation_id),
                            )
                        else:
                            cur = db.execute(
                                """
                                INSERT INTO wa_conversations
                                    (wa_number_id, contact_wa_id, contact_name, last_message,
                                     last_message_at, last_inbound_at, unread_count, updated_at)
                                VALUES (?, ?, ?, ?, ?, NULL, 0, ?)
                                """,
                                (wa_number["id"], contact_wa_id, contact_name, body, sent_at, now_iso),
                            )
                            conversation_id = cur.lastrowid

                        db.execute(
                            """
                            INSERT INTO wa_messages (
                                conversation_id, meta_message_id, direction, message_type, body,
                                media_url, meta_media_id, media_mime_type, media_filename,
                                sent_at, status, status_error, sent_by_user_id, sent_by_username, created_at
                            ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, 'sent', NULL, NULL, 'WhatsApp Business', ?)
                            """,
                            (conversation_id, meta_message_id or None, msg_type, body, media_url,
                             meta_media_id, media_mime_type, media_filename, sent_at, now_iso),
                        )
        db.commit()
    except Exception as exc:
        db.rollback()
        print("Gupshup webhook processing error:", repr(exc))
        return "", 500
    return "", 200



# =========================
# WhatsApp branches - operational unified inbox
# =========================
@app.route("/whatsapp")
@login_required
def whatsapp_branches():
    perms = session.get("perms") or {}
    is_master = session.get("username") == MASTER_USERNAME
    if not is_master and not bool(perms.get("whatsapp_branches")):
        return render_template("no_permission.html"), 403

    db = get_db()
    ensure_whatsapp_schema(db)
    allowed_ids = _wa_allowed_number_ids(db, require_reply=False)
    reply_ids = set(_wa_allowed_number_ids(db, require_reply=True))

    selected_number = (request.args.get("number") or "all").strip()
    search_text = (request.args.get("q") or "").strip()
    conversation_id = (request.args.get("conversation") or "").strip()
    show_deleted = bool(is_master and request.args.get("trash") == "1")
    # Token to prevent cross-site destructive form requests.
    if is_master and not session.get("_wa_delete_token"):
        session["_wa_delete_token"] = uuid.uuid4().hex

    if not allowed_ids:
        numbers = []
        conversations = []
    else:
        placeholders = ",".join(["?"] * len(allowed_ids))
        numbers = db.execute(
            f"""
            SELECT id, label, branch_code, phone_number, meta_phone_number_id, is_active, sort_order
            FROM wa_numbers
            WHERE is_active=1 AND id IN ({placeholders})
            ORDER BY sort_order ASC, id ASC
            """,
            allowed_ids,
        ).fetchall()

        where = [f"c.wa_number_id IN ({placeholders})", "c.is_deleted=?"]
        params = list(allowed_ids) + [1 if show_deleted else 0]
        if selected_number != "all":
            try:
                selected_id = int(selected_number)
                if selected_id in allowed_ids:
                    where.append("c.wa_number_id=?")
                    params.append(selected_id)
                else:
                    selected_number = "all"
            except ValueError:
                selected_number = "all"
        if search_text:
            where.append("(COALESCE(c.contact_name,'') LIKE ? OR COALESCE(c.contact_wa_id,'') LIKE ? OR COALESCE(c.last_message,'') LIKE ?)")
            term = f"%{search_text}%"
            params.extend([term, term, term])

        conversations = db.execute(
            f"""
            SELECT c.id, c.wa_number_id, c.contact_wa_id, c.contact_name,
                   c.last_message, c.last_message_at, c.last_inbound_at,
                   c.unread_count, n.label AS number_label, n.branch_code
            FROM wa_conversations c
            JOIN wa_numbers n ON n.id=c.wa_number_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(c.last_message_at, c.updated_at) DESC, c.id DESC
            LIMIT 300
            """,
            params,
        ).fetchall()

    active_conversation = None
    messages = []
    can_reply_active = False
    freeform_open = False
    freeform_expires_at = None
    latest_message_id = 0
    approved_templates = []
    template_load_error = None

    if conversation_id:
        try:
            cid = int(conversation_id)
            if allowed_ids:
                placeholders = ",".join(["?"] * len(allowed_ids))
                active_conversation = db.execute(
                    f"""
                    SELECT c.*, n.label AS number_label, n.branch_code,
                           n.meta_phone_number_id, n.phone_number AS business_phone
                    FROM wa_conversations c
                    JOIN wa_numbers n ON n.id=c.wa_number_id
                    WHERE c.id=? AND c.wa_number_id IN ({placeholders}) AND c.is_deleted=?
                    """,
                    [cid] + list(allowed_ids) + [1 if show_deleted else 0],
                ).fetchone()
            if active_conversation:
                messages = db.execute(
                    """
                    SELECT id, meta_message_id, direction, message_type, body, media_url,
                           meta_media_id, media_mime_type, media_filename, sent_at, status,
                           status_error, sent_by_user_id, sent_by_username
                    FROM wa_messages
                    WHERE conversation_id=?
                    ORDER BY COALESCE(sent_at, created_at) ASC, id ASC
                    LIMIT 1200
                    """,
                    (cid,),
                ).fetchall()
                latest_message_id = int(messages[-1]["id"]) if messages else 0
                can_reply_active = (not show_deleted and int(active_conversation["wa_number_id"]) in reply_ids)
                freeform_open, freeform_expires_at = _wa_freeform_open(active_conversation["last_inbound_at"])
                # No automatic template fetch: ordinary replies are shown only inside
                # the official 24h service window. Template diagnostics remain available
                # separately for administrators from the existing settings page.

                latest_incoming = db.execute(
                    """
                    SELECT meta_message_id FROM wa_messages
                    WHERE conversation_id=? AND direction='in' AND meta_message_id IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (cid,),
                ).fetchone()
                db.execute("UPDATE wa_conversations SET unread_count=0 WHERE id=?", (cid,))
                db.commit()
                if latest_incoming and active_conversation["meta_phone_number_id"]:
                    _wa_mark_read(active_conversation["meta_phone_number_id"], latest_incoming["meta_message_id"])
        except ValueError:
            pass

    draft_payload = session.get('_debt_wa_draft') or {}
    debt_draft_text = ''
    if active_conversation and not show_deleted and draft_payload.get('conversation_id') == active_conversation['id']:
        debt_draft_text = str(draft_payload.get('text') or '')
        session.pop('_debt_wa_draft',None)

    connected_count = sum(1 for n in numbers if _wa_digits(n["phone_number"]) or (n["meta_phone_number_id"] or "").strip()) if numbers else 0
    total_unread = sum(int(c["unread_count"] or 0) for c in conversations) if conversations else 0

    html = render_template(
        "whatsapp_branches.html",
        numbers=numbers,
        conversations=conversations,
        active_conversation=active_conversation,
        messages=messages,
        selected_number=selected_number,
        search_text=search_text,
        connected_count=connected_count,
        total_unread=total_unread,
        can_manage_numbers=(is_master or bool(perms.get("whatsapp_numbers_manage"))),
        can_reply_active=can_reply_active,
        freeform_open=freeform_open,
        freeform_expires_at=freeform_expires_at,
        latest_message_id=latest_message_id,
        api_ready=bool(_wa_access_token() or _gupshup_any_api_ready()),
        reply_number_ids=list(reply_ids),
        approved_templates=approved_templates,
        template_load_error=template_load_error,
        debt_draft_text=debt_draft_text,
        is_master=is_master,
        show_deleted=show_deleted,
        wa_delete_token=session.get("_wa_delete_token") if is_master else "",
    )
    # Keep all other WhatsApp functionality unchanged.  An active 24-hour
    # window receives the debt message in the normal composer.  Otherwise,
    # preserve a visible read-only draft: opening the chat is NOT a send.
    if debt_draft_text:
        from markupsafe import escape as escape_html
        escaped_draft = str(escape_html(debt_draft_text))
        marker = 'id="waMessage"'
        found = html.find(marker)
        if found >= 0:
            content_start = html.find('>', found) + 1
            content_end = html.find('</textarea>', content_start)
            if content_start > 0 and content_end >= content_start:
                html = html[:content_start] + escaped_draft + html[content_end:]
        else:
            # There is no message composer until the customer opens a
            # service window. Show the prepared text without a send control.
            draft_preview = (
                '<div class="wa-composer" style="border:1px solid #eab308; padding:12px">'
                '<div class="wa-window-note" style="color:#facc15">'
                'مسودة المطالبة من دفتر الديون — لم يتم إرسالها. '
                'يتطلب إرسال الرسالة خارج نافذة 24 ساعة قالبًا معتمدًا وحالة استخدام مسموحة.</div>'
                '<label for="debtWaReadonlyDraft" class="form-label small">نص المسودة</label>'
                '<textarea id="debtWaReadonlyDraft" class="form-control" '
                'rows="7" readonly></textarea>'
                '<button id="copyDebtWaDraft" type="button" '
                'class="btn btn-outline-info btn-sm mt-2">نسخ نص المسودة</button>'
                '</div>'
            )
            # Insert the preview in the existing chat section (and nowhere
            # else), even when no API key / approved template is available.
            chat_start = html.find('<section class="wa-chat">')
            chat_end = html.find('</section>', chat_start) if chat_start >= 0 else -1
            if chat_end >= 0:
                html = html[:chat_end] + draft_preview + html[chat_end:]
                from json import dumps as json_dumps
                draft_json = json_dumps(debt_draft_text, ensure_ascii=False)
                # Escape any HTML-closing delimiter when placed in a JS string.
                draft_json = draft_json.replace('<', r'\u003c')
                script = (
                    '<script>(function(){const v=' + draft_json + ';'
                    'const e=document.getElementById("debtWaReadonlyDraft");'
                    'if(e)e.value=v;'
                    'const b=document.getElementById("copyDebtWaDraft");'
                    'if(b)b.addEventListener("click",function(){'
                    'if(navigator.clipboard)navigator.clipboard.writeText(v);'
                    'else if(e){e.select();document.execCommand("copy");}'
                    '});})();</script>'
                )
                html += script
    return html



@app.route("/whatsapp/conversation/<int:conversation_id>/delete", methods=["POST"])
@login_required
def whatsapp_admin_delete_conversation(conversation_id):
    """Move the conversation out of the inbox, preserving WhatsApp history."""
    if session.get("username") != MASTER_USERNAME:
        return render_template("no_permission.html"), 403
    expected = session.get("_wa_delete_token") or ""
    provided = request.form.get("csrf_token") or ""
    if not expected or not hmac.compare_digest(expected, provided):
        return "طلب غير صالح. حدّث الصفحة ثم حاول مجددًا.", 400
    db = get_db()
    ensure_whatsapp_schema(db)
    row = db.execute("SELECT id FROM wa_conversations WHERE id=? AND is_deleted=0", (conversation_id,)).fetchone()
    if not row:
        flash("المحادثة غير موجودة أو محذوفة سابقًا.", "warning")
        return redirect(url_for("whatsapp_branches"))
    db.execute("""
        UPDATE wa_conversations SET is_deleted=1,deleted_at=datetime('now'),
           deleted_by=?,unread_count=0 WHERE id=?
    """, (session.get("username") or MASTER_USERNAME, conversation_id))
    db.commit()
    flash("حُذفت المحادثة من القائمة ونُقلت للمحذوفات. يمكن للإدارة استعادتها، ولم تُحذف الرسائل من السجل.", "success")
    return redirect(url_for("whatsapp_branches"))


@app.route("/whatsapp/conversation/<int:conversation_id>/restore", methods=["POST"])
@login_required
def whatsapp_admin_restore_conversation(conversation_id):
    if session.get("username") != MASTER_USERNAME:
        return render_template("no_permission.html"), 403
    expected = session.get("_wa_delete_token") or ""
    provided = request.form.get("csrf_token") or ""
    if not expected or not hmac.compare_digest(expected, provided):
        return "طلب غير صالح. حدّث الصفحة ثم حاول مجددًا.", 400
    db = get_db()
    ensure_whatsapp_schema(db)
    changed = db.execute("""
        UPDATE wa_conversations SET is_deleted=0,deleted_at=NULL,deleted_by=NULL,
            updated_at=datetime('now') WHERE id=? AND is_deleted=1
    """, (conversation_id,)).rowcount
    db.commit()
    flash("تمت استعادة المحادثة إلى لوحة واتساب." if changed else "المحادثة غير موجودة في المحذوفات.",
          "success" if changed else "warning")
    if changed:
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))
    return redirect(url_for("whatsapp_branches", trash=1))


@app.route("/whatsapp/new", methods=["POST"])
@login_required
def whatsapp_new_conversation():
    db = get_db()
    ensure_whatsapp_schema(db)
    reply_ids = _wa_allowed_number_ids(db, require_reply=True)
    if not reply_ids:
        return render_template("no_permission.html"), 403
    try:
        wa_number_id = int(request.form.get("wa_number_id") or 0)
    except ValueError:
        wa_number_id = 0
    if wa_number_id not in reply_ids:
        flash("ليس لديك صلاحية الرد من رقم واتساب المحدد", "danger")
        return redirect(url_for("whatsapp_branches"))

    phone = _wa_normalize_destination(request.form.get("phone"))
    contact_name = (request.form.get("contact_name") or "").strip()
    if not phone:
        flash("أدخل رقم جوال صحيحًا مع مفتاح الدولة، أو رقم سعودي يبدأ بـ 05", "warning")
        return redirect(url_for("whatsapp_branches"))

    n = db.execute("SELECT id, phone_number FROM wa_numbers WHERE id=? AND is_active=1", (wa_number_id,)).fetchone()
    if not n:
        flash("رقم واتساب المرسل غير متاح", "danger")
        return redirect(url_for("whatsapp_branches"))
    if _wa_digits(n["phone_number"]) == phone:
        flash("لا يمكن فتح محادثة مع نفس رقم المؤسسة", "warning")
        return redirect(url_for("whatsapp_branches"))

    row = db.execute(
        "SELECT id, contact_name FROM wa_conversations WHERE wa_number_id=? AND contact_wa_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1",
        (wa_number_id, phone),
    ).fetchone()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if row:
        cid = int(row["id"])
        if contact_name and contact_name != (row["contact_name"] or ""):
            db.execute("UPDATE wa_conversations SET contact_name=?, updated_at=? WHERE id=?", (contact_name, now_iso, cid))
            db.commit()
    else:
        cur = db.execute(
            """
            INSERT INTO wa_conversations
                (wa_number_id, contact_wa_id, contact_name, last_message, last_message_at, last_inbound_at, unread_count, updated_at)
            VALUES (?, ?, ?, ?, NULL, NULL, 0, ?)
            """,
            (wa_number_id, phone, contact_name or None, "محادثة جديدة — لم يتم إرسال رسالة بعد", now_iso),
        )
        db.commit()
        cid = int(cur.lastrowid)
        flash("تم فتح المحادثة. إذا لم يرسل العميل خلال آخر 24 ساعة فابدأ بقالب WhatsApp معتمد.", "success")
    return redirect(url_for("whatsapp_branches", conversation=cid, number=wa_number_id))


@app.route("/whatsapp/conversation/<int:conversation_id>/send-template", methods=["POST"])
@login_required
def whatsapp_send_template(conversation_id):
    db = get_db()
    ensure_whatsapp_schema(db)
    reply_ids = _wa_allowed_number_ids(db, require_reply=True)
    if not reply_ids:
        return render_template("no_permission.html"), 403
    placeholders = ",".join(["?"] * len(reply_ids))
    conv = db.execute(
        f"""
        SELECT c.*, n.label AS number_label, n.branch_code, n.phone_number
        FROM wa_conversations c JOIN wa_numbers n ON n.id=c.wa_number_id
        WHERE c.id=? AND c.wa_number_id IN ({placeholders}) AND c.is_deleted=0
        """,
        [conversation_id] + list(reply_ids),
    ).fetchone()
    if not conv:
        return render_template("no_permission.html"), 403

    template_id = (request.form.get("template_id") or "").strip()
    params_raw = (request.form.get("template_params") or "").strip()
    params = [x.strip() for x in params_raw.split("|")] if params_raw else []
    approved, err = _gupshup_get_approved_text_templates(conv["branch_code"] or "")
    if err:
        flash(err, "danger")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))
    allowed = {t["id"]: t for t in approved}
    selected = allowed.get(template_id)
    if not selected:
        flash("اختر قالبًا معتمدًا من Gupshup", "warning")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))
    expected = int(selected.get("param_count") or 0)
    if expected and len(params) != expected:
        flash(f"هذا القالب يحتاج {expected} قيمة للمتغيرات. افصل القيم بعلامة |", "warning")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    mid, send_err = _gupshup_post_template(
        conv["branch_code"] or "", conv["phone_number"], conv["contact_wa_id"], template_id, params
    )
    if send_err:
        flash(f"تعذر إرسال القالب: {send_err}", "danger")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    display = f"📨 قالب: {selected['name']}"
    if params:
        display += " — " + " | ".join(params)
    db.execute(
        """
        INSERT INTO wa_messages
            (conversation_id, meta_message_id, direction, message_type, body, sent_at, status,
             sent_by_user_id, sent_by_username, created_at)
        VALUES (?, ?, 'out', 'template', ?, ?, 'submitted', ?, ?, ?)
        """,
        (conversation_id, mid, display, now_iso, session.get("user_id"), session.get("username") or "", now_iso),
    )
    db.execute(
        "UPDATE wa_conversations SET last_message=?, last_message_at=?, updated_at=? WHERE id=?",
        (display, now_iso, now_iso, conversation_id),
    )
    db.commit()
    flash("تم إرسال القالب لبدء المحادثة. بعد رد العميل يمكنك الكتابة بحرية لمدة 24 ساعة.", "success")
    return redirect(url_for("whatsapp_branches", conversation=conversation_id))


@app.route("/whatsapp/conversation/<int:conversation_id>/send", methods=["POST"])
@login_required
def whatsapp_send_message(conversation_id):
    db = get_db()
    ensure_whatsapp_schema(db)
    reply_ids = _wa_allowed_number_ids(db, require_reply=True)
    if not reply_ids:
        return render_template("no_permission.html"), 403

    placeholders = ",".join(["?"] * len(reply_ids))
    conv = db.execute(
        f"""
        SELECT c.*, n.label AS number_label, n.branch_code, n.phone_number,
               n.meta_phone_number_id
        FROM wa_conversations c
        JOIN wa_numbers n ON n.id=c.wa_number_id
        WHERE c.id=? AND c.wa_number_id IN ({placeholders}) AND c.is_deleted=0
        """,
        [conversation_id] + list(reply_ids),
    ).fetchone()
    if not conv:
        return render_template("no_permission.html"), 403

    branch_code = conv["branch_code"] or ""
    use_gupshup = bool(_gupshup_api_key(branch_code))
    if not use_gupshup and not (conv["meta_phone_number_id"] or "").strip():
        flash("هذا الرقم غير مربوط بـ Gupshup أو Meta للإرسال", "danger")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))
    if not use_gupshup and not _wa_access_token():
        flash("أضف GUPSHUP_API_KEY أو WHATSAPP_META_ACCESS_TOKEN في Render أولاً", "danger")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    freeform_open, _ = _wa_freeform_open(conv["last_inbound_at"])
    if not freeform_open:
        flash("انتهت نافذة خدمة العميل (24 ساعة). لا يمكن إرسال رسالة عادية؛ يلزم قالب WhatsApp معتمد.", "warning")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    text_body = (request.form.get("message") or "").strip()
    attachment = request.files.get("attachment")
    has_attachment = bool(attachment and attachment.filename)
    if not text_body and not has_attachment:
        flash("اكتب رسالة أو اختر مرفقاً", "warning")
        return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    recipient = _wa_digits(conv["contact_wa_id"])
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message_type = "text"
    media_id = None
    media_mime = None
    media_filename = None
    stored_media_ref = None
    display_body = text_body

    if use_gupshup:
        if has_attachment:
            stored_name, media_mime, media_filename, err = _wa_save_outbound_media(attachment)
            if err:
                flash(err, "danger")
                return redirect(url_for("whatsapp_branches", conversation=conversation_id))
            public_url = _wa_public_media_url(stored_name)
            stored_media_ref = "local:" + stored_name
            if media_mime.startswith("image/"):
                message_type = "image"
                message_obj = {"type": "image", "originalUrl": public_url, "previewUrl": public_url}
                if text_body:
                    message_obj["caption"] = text_body
            elif media_mime.startswith("video/"):
                message_type = "video"
                message_obj = {"type": "video", "url": public_url, "previewUrl": public_url}
                if text_body:
                    message_obj["caption"] = text_body
            elif media_mime.startswith("audio/"):
                message_type = "audio"
                message_obj = {"type": "audio", "url": public_url}
            else:
                message_type = "document"
                message_obj = {"type": "file", "url": public_url, "filename": media_filename}
                if text_body:
                    message_obj["caption"] = text_body
            labels = {"image":"📷 صورة", "video":"🎥 فيديو", "audio":"🎵 رسالة صوتية", "document":"📄 مستند"}
            display_body = labels[message_type]
            if text_body:
                display_body += f": {text_body}"
            elif media_filename and message_type == "document":
                display_body += f": {media_filename}"
        else:
            message_obj = {"type": "text", "text": text_body, "previewUrl": True}

        meta_message_id, err = _gupshup_post_message(
            branch_code,
            conv["phone_number"],
            recipient,
            message_obj,
        )
        if err:
            flash(f"تعذر إرسال الرسالة عبر Gupshup: {err}", "danger")
            return redirect(url_for("whatsapp_branches", conversation=conversation_id))
    else:
        phone_number_id = (conv["meta_phone_number_id"] or "").strip()
        if has_attachment:
            media_id, media_mime, media_filename, err = _wa_upload_media(phone_number_id, attachment)
            if err:
                flash(f"تعذر رفع المرفق إلى Meta: {err}", "danger")
                return redirect(url_for("whatsapp_branches", conversation=conversation_id))
            if media_mime.startswith("image/"):
                message_type = "image"
            elif media_mime.startswith("video/"):
                message_type = "video"
            elif media_mime.startswith("audio/"):
                message_type = "audio"
            else:
                message_type = "document"
            media_obj = {"id": media_id}
            if message_type in ("image", "video") and text_body:
                media_obj["caption"] = text_body
            if message_type == "document":
                media_obj["filename"] = media_filename
                if text_body:
                    media_obj["caption"] = text_body
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": message_type,
                message_type: media_obj,
            }
            labels = {"image":"📷 صورة", "video":"🎥 فيديو", "audio":"🎵 رسالة صوتية", "document":"📄 مستند"}
            display_body = labels[message_type]
            if text_body:
                display_body += f": {text_body}"
            elif media_filename and message_type == "document":
                display_body += f": {media_filename}"
        else:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": True, "body": text_body},
            }
        meta_message_id, err = _wa_send_payload(phone_number_id, payload)
        if err:
            flash(f"تعذر إرسال الرسالة: {err}", "danger")
            return redirect(url_for("whatsapp_branches", conversation=conversation_id))

    username = session.get("username") or ""
    user_id = session.get("user_id")
    db.execute(
        """
        INSERT INTO wa_messages (
            conversation_id, meta_message_id, direction, message_type, body,
            media_url, meta_media_id, media_mime_type, media_filename,
            sent_at, status, status_error, sent_by_user_id, sent_by_username, created_at
        ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, 'submitted', NULL, ?, ?, ?)
        """,
        (conversation_id, meta_message_id, message_type, display_body, stored_media_ref,
         media_id, media_mime, media_filename, now_iso, user_id, username, now_iso),
    )
    db.execute(
        "UPDATE wa_conversations SET last_message=?, last_message_at=?, updated_at=? WHERE id=?",
        (display_body, now_iso, now_iso, conversation_id),
    )
    db.commit()
    return redirect(url_for("whatsapp_branches", conversation=conversation_id))


@app.route("/whatsapp/public-media/<path:filename>")
def whatsapp_public_media(filename):
    """Short-lived signed URL used only so Gupshup can fetch an outbound attachment."""
    try:
        expires = int(request.args.get("exp") or 0)
    except ValueError:
        expires = 0
    sig = (request.args.get("sig") or "").strip()
    if not expires or expires < int(time.time()):
        return "Expired", 410
    expected = _wa_media_signature(filename, expires)
    if not sig or not hmac.compare_digest(sig, expected):
        return "Forbidden", 403
    return send_from_directory(WA_MEDIA_DIR, filename, max_age=60)


@app.route("/whatsapp/media/<int:message_id>")
@login_required
def whatsapp_media(message_id):
    db = get_db()
    ensure_whatsapp_schema(db)
    allowed_ids = _wa_allowed_number_ids(db, require_reply=False)
    if not allowed_ids:
        return render_template("no_permission.html"), 403

    placeholders = ",".join(["?"] * len(allowed_ids))
    row = db.execute(
        f"""
        SELECT m.id, m.message_type, m.media_url, m.meta_media_id, m.media_mime_type, m.media_filename,
               c.wa_number_id, n.branch_code, n.meta_phone_number_id
        FROM wa_messages m
        JOIN wa_conversations c ON c.id=m.conversation_id
        JOIN wa_numbers n ON n.id=c.wa_number_id
        WHERE m.id=? AND c.wa_number_id IN ({placeholders})
        """,
        [message_id] + list(allowed_ids),
    ).fetchone()
    if not row:
        return "Media not found", 404

    media_url = str(row["media_url"] or "").strip()
    if media_url.startswith("local:"):
        filename = media_url.split(":", 1)[1]
        return send_from_directory(
            WA_MEDIA_DIR,
            filename,
            as_attachment=(row["message_type"] == "document"),
            download_name=row["media_filename"] or filename,
            max_age=60,
        )

    if media_url.startswith("http://") or media_url.startswith("https://"):
        try:
            req = urllib.request.Request(media_url, headers={"User-Agent": "AlEshanWhatsApp/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                content = resp.read()
                content_type = resp.headers.get("Content-Type") or row["media_mime_type"] or "application/octet-stream"
            return send_file(
                BytesIO(content),
                mimetype=content_type,
                as_attachment=(row["message_type"] == "document"),
                download_name=row["media_filename"] or f"whatsapp-{message_id}",
                max_age=60,
            )
        except Exception as exc:
            print("Gupshup media URL download failed:", repr(exc))

    # Legacy direct-Meta media fallback.
    if row["meta_media_id"] and _wa_access_token() and row["meta_phone_number_id"]:
        info, err = _wa_graph_request(
            "GET",
            f"{row['meta_media_id']}?phone_number_id={row['meta_phone_number_id']}",
        )
        if not err and info and info.get("url"):
            raw, err = _wa_graph_request("GET", info["url"], timeout=45)
            if not err and raw and "raw" in raw:
                mime = row["media_mime_type"] or info.get("mime_type") or raw.get("content_type") or "application/octet-stream"
                return send_file(
                    BytesIO(raw["raw"]),
                    mimetype=mime,
                    as_attachment=(row["message_type"] == "document"),
                    download_name=row["media_filename"] or f"whatsapp-{message_id}",
                    max_age=60,
                )

    return "المرفق وصل كـ Media ID فقط. أضف GUPSHUP_APP_TOKEN لاحقًا لتفعيل تنزيل وسائط Coexistence من Gupshup.", 404


@app.route("/whatsapp/api/state")
@login_required
def whatsapp_api_state():
    db = get_db()
    ensure_whatsapp_schema(db)
    allowed_ids = _wa_allowed_number_ids(db, require_reply=False)
    if not allowed_ids:
        return {"total_unread": 0, "latest_message_id": 0}
    placeholders = ",".join(["?"] * len(allowed_ids))
    unread = db.execute(
        f"SELECT COALESCE(SUM(unread_count),0) AS c FROM wa_conversations WHERE wa_number_id IN ({placeholders}) AND is_deleted=0",
        allowed_ids,
    ).fetchone()["c"]
    latest_id = 0
    conv_id = request.args.get("conversation")
    if conv_id:
        try:
            cid = int(conv_id)
            row = db.execute(
                f"""
                SELECT COALESCE(MAX(m.id),0) AS mid
                FROM wa_messages m JOIN wa_conversations c ON c.id=m.conversation_id
                WHERE c.id=? AND c.wa_number_id IN ({placeholders}) AND c.is_deleted=0
                """,
                [cid] + list(allowed_ids),
            ).fetchone()
            latest_id = int(row["mid"] or 0)
        except ValueError:
            pass
    return {"total_unread": int(unread or 0), "latest_message_id": latest_id}


@app.route("/whatsapp/numbers", methods=["GET", "POST"])
@login_required
def whatsapp_numbers_manage():
    perms = session.get("perms") or {}
    is_master = session.get("username") == MASTER_USERNAME
    if not is_master and not bool(perms.get("whatsapp_numbers_manage")):
        return render_template("no_permission.html"), 403

    db = get_db()
    ensure_whatsapp_schema(db)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "update_number":
            try:
                number_id = int(request.form.get("number_id", "0") or 0)
            except ValueError:
                number_id = 0
            row = db.execute("SELECT id FROM wa_numbers WHERE id=?", (number_id,)).fetchone()
            if not row:
                flash("رقم واتساب غير موجود", "danger")
                return redirect(url_for("whatsapp_numbers_manage"))

            label = (request.form.get("label") or "").strip()
            branch_code = (request.form.get("branch_code") or "").strip()
            phone_number = (request.form.get("phone_number") or "").strip()
            meta_phone_number_id = (request.form.get("meta_phone_number_id") or "").strip()
            if not label:
                flash("اسم الرقم مطلوب", "warning")
                return redirect(url_for("whatsapp_numbers_manage"))
            try:
                sort_order = int(request.form.get("sort_order", "0") or 0)
            except ValueError:
                sort_order = 0
            sort_order = max(0, min(sort_order, 9999))
            is_active = 1 if request.form.get("is_active") == "on" else 0
            db.execute(
                """
                UPDATE wa_numbers
                   SET label=?, branch_code=?, phone_number=?, meta_phone_number_id=?,
                       sort_order=?, is_active=?
                 WHERE id=?
                """,
                (label, branch_code, phone_number, meta_phone_number_id,
                 sort_order, is_active, number_id),
            )
            db.commit()
            flash(f"تم تحديث {label} بنجاح", "success")
            return redirect(url_for("whatsapp_numbers_manage"))

        if action == "save_number_access":
            try:
                uid = int(request.form.get("user_id", "0") or 0)
            except ValueError:
                uid = 0
            user = db.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
            if not user or user["username"] == MASTER_USERNAME:
                flash("المستخدم غير صالح لتخصيص أرقام واتساب", "warning")
                return redirect(url_for("whatsapp_numbers_manage"))

            all_numbers = db.execute("SELECT id FROM wa_numbers ORDER BY id").fetchall()
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for n in all_numbers:
                nid = int(n["id"])
                can_view = 1 if request.form.get(f"view_{nid}") == "on" else 0
                can_reply = 1 if request.form.get(f"reply_{nid}") == "on" else 0
                if can_reply:
                    can_view = 1
                db.execute(
                    """
                    INSERT INTO wa_number_permissions (user_id, wa_number_id, can_view, can_reply, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, wa_number_id)
                    DO UPDATE SET can_view=excluded.can_view,
                                  can_reply=excluded.can_reply,
                                  updated_at=excluded.updated_at
                    """,
                    (uid, nid, can_view, can_reply, now_iso),
                )
            db.commit()
            flash(f"تم تحديث صلاحيات أرقام واتساب للمستخدم {user['username']}", "success")
            return redirect(url_for("whatsapp_numbers_manage") + f"#user-{uid}")

    numbers = db.execute(
        """
        SELECT id, label, branch_code, phone_number, meta_phone_number_id,
               is_active, sort_order, created_at
        FROM wa_numbers ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    users = db.execute(
        "SELECT id, username FROM users ORDER BY CASE WHEN username=? THEN 0 ELSE 1 END, username COLLATE NOCASE",
        (MASTER_USERNAME,),
    ).fetchall()
    access_rows = db.execute(
        "SELECT user_id, wa_number_id, can_view, can_reply FROM wa_number_permissions"
    ).fetchall()
    access_map = {}
    configured_users = set()
    for r in access_rows:
        configured_users.add(int(r["user_id"]))
        access_map[(int(r["user_id"]), int(r["wa_number_id"]))] = {
            "can_view": bool(r["can_view"]),
            "can_reply": bool(r["can_reply"]),
        }

    return render_template(
        "whatsapp_numbers_manage.html",
        numbers=numbers,
        users=users,
        access_map=access_map,
        configured_users=configured_users,
        webhook_callback_url=request.url_root.rstrip("/") + url_for("gupshup_webhook"),
        webhook_verify_token_is_set=bool(_wa_webhook_verify_token()),
        webhook_app_secret_is_set=bool(_wa_meta_app_secret()),
        whatsapp_access_token_is_set=bool(_wa_access_token() or _gupshup_any_api_ready()),
        whatsapp_graph_version=_wa_graph_version(),
    )

@app.route("/whatsapp/connection", methods=["GET", "POST"])
@login_required
def whatsapp_connection_settings():
    """Safe configuration and read-only template probe for the Gupshup sender.

    Changes provider app metadata only. Never sends WhatsApp messages.
    """
    if session.get("username") != MASTER_USERNAME:
        return render_template("no_permission.html"), 403
    db = get_db()
    ensure_whatsapp_schema(db)
    if request.method == "POST":
        try:
            number_id = int(request.form.get("number_id") or 0)
        except (TypeError, ValueError):
            number_id = 0
        found = db.execute("SELECT id, label FROM wa_numbers WHERE id=?", (number_id,)).fetchone()
        if not found:
            flash("الرقم المحدد غير موجود.", "danger")
            return redirect(url_for("whatsapp_connection_settings"))
        app_id = (request.form.get("gupshup_app_id") or "").strip()
        app_name = (request.form.get("gupshup_app_name") or "").strip()
        if (app_id and not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", app_id)) or (
                app_name and not re.fullmatch(r"[A-Za-z0-9_. -]{2,100}", app_name)):
            flash("تحقق من App ID واسم التطبيق كما يظهران في إعدادات Gupshup.", "danger")
            return redirect(url_for("whatsapp_connection_settings"))
        db.execute("""UPDATE wa_numbers SET gupshup_app_id=?,gupshup_app_name=? WHERE id=?""",
                   (app_id or None, app_name or None, number_id))
        db.commit()
        flash("تم حفظ بيانات التطبيق لهذا الرقم. لم تُرسل رسائل.", "success")
        return redirect(url_for("whatsapp_connection_settings", check=number_id))

    numbers = db.execute("SELECT * FROM wa_numbers ORDER BY sort_order,id").fetchall()
    providers = []
    for n in numbers:
        branch = n["branch_code"] or ""
        providers.append({
            "id": n["id"], "label": n["label"], "branch": branch,
            "phone": n["phone_number"] or "", "is_active": bool(n["is_active"]),
            "app_id_saved": n["gupshup_app_id"] or "",
            "app_name_saved": n["gupshup_app_name"] or "",
            "app_id_effective": _gupshup_app_id(branch),
            "app_name_effective": _gupshup_app_name(branch),
            "has_api_key": bool(_gupshup_api_key(branch)),
            "has_source": bool(_gupshup_source_number(branch, n["phone_number"])),
        })
    checked = None
    raw_check = request.args.get("check", "").strip()
    if raw_check:
        try:
            check_id = int(raw_check)
        except (ValueError, TypeError):
            check_id = 0
        selected = next((x for x in providers if int(x["id"]) == check_id), None)
        if selected:
            templates, err = _gupshup_get_approved_text_templates(selected["branch"])
            checked = {"number": selected, "error": err,
                       "count": len(templates),
                       "templates": templates[:15]}
    return render_template("whatsapp_connection_settings.html",
                           providers=providers, checked=checked)


# =========================
# Follow-up Dashboard (Power BI)
# =========================
@app.route("/dashboard")
@login_required
@permission_required("dashboard")
def dashboard():
    return render_template("dashboard.html")


# =========================
# Accounting rejection alerts
# =========================
@app.route("/accounting/rejection-alerts")
@login_required
def accounting_rejection_alerts():
    perms = session.get("perms") or {}
    is_master = session.get("username") == MASTER_USERNAME

    if not is_master and not bool(perms.get("accounting_rejection_alerts")):
        return render_template("no_permission.html"), 403

    alerts = _accounting_alert_rows()
    return render_template(
        "accounting_rejection_alerts.html",
        alerts=alerts,
    )


# =========================
# Home Slider Management
# =========================
@app.route("/home-slides/file/<path:filename>")
@login_required
def home_slide_file(filename):
    return send_from_directory(
        SLIDES_DIR,
        filename
    )


@app.route("/home-slides", methods=["GET", "POST"])
@login_required
@permission_required("home_slides")
def home_slides():
    db = get_db()
    ensure_home_slides_schema(db)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        slide_id = (request.form.get("id") or "").strip()

        # =========================
        # رفع صورة / عدة صور
        # =========================
        if action == "upload":
            files = request.files.getlist("images")
            uploaded_count = 0

            max_order_row = db.execute("""
                SELECT COALESCE(MAX(sort_order), 0) AS m
                FROM home_slides
            """).fetchone()

            next_order = int(max_order_row["m"] or 0)

            for file in files:
                if not file or not file.filename:
                    continue

                if not allowed_slide_file(file.filename):
                    flash(
                        f"صيغة الملف غير مدعومة: {file.filename}",
                        "warning"
                    )
                    continue

                safe_name = secure_filename(file.filename)
                extension = file.filename.rsplit(".", 1)[1].lower()

                # اسم عشوائي يمنع التعارض حتى لو تكرر اسم الصورة
                new_filename = f"{uuid.uuid4().hex}.{extension}"
                save_path = os.path.join(SLIDES_DIR, new_filename)

                try:
                    file.save(save_path)
                except Exception as e:
                    print("home slide upload error:", e)
                    flash(
                        f"تعذر رفع الصورة: {file.filename}",
                        "danger"
                    )
                    continue

                next_order += 1

                db.execute("""
                    INSERT INTO home_slides (
                        filename,
                        original_name,
                        sort_order,
                        is_active,
                        created_at
                    )
                    VALUES (?, ?, ?, 1, datetime('now'))
                """, (
                    new_filename,
                    safe_name or file.filename,
                    next_order
                ))

                uploaded_count += 1

            db.commit()

            if uploaded_count:
                flash(
                    f"تم رفع {uploaded_count} صورة بنجاح",
                    "success"
                )
            elif not files:
                flash("اختر صورة واحدة على الأقل", "warning")

        # =========================
        # حذف
        # =========================
        elif action == "delete" and slide_id:
            row = db.execute("""
                SELECT filename
                FROM home_slides
                WHERE id=?
            """, (slide_id,)).fetchone()

            if row:
                file_path = os.path.join(
                    SLIDES_DIR,
                    row["filename"]
                )

                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                except Exception as e:
                    # لا نمنع حذف السجل إذا تعذر حذف الملف
                    print("home slide delete file error:", e)

                db.execute(
                    "DELETE FROM home_slides WHERE id=?",
                    (slide_id,)
                )
                db.commit()
                flash("تم حذف الصورة", "warning")

        # =========================
        # إظهار / إخفاء
        # =========================
        elif action == "toggle_active" and slide_id:
            row = db.execute("""
                SELECT is_active
                FROM home_slides
                WHERE id=?
            """, (slide_id,)).fetchone()

            if row:
                new_value = 0 if int(row["is_active"] or 0) == 1 else 1

                db.execute("""
                    UPDATE home_slides
                    SET is_active=?
                    WHERE id=?
                """, (
                    new_value,
                    slide_id
                ))
                db.commit()

        # =========================
        # ترتيب أعلى / أسفل
        # =========================
        elif action in ("move_up", "move_down") and slide_id:
            row = db.execute("""
                SELECT id, sort_order
                FROM home_slides
                WHERE id=?
            """, (slide_id,)).fetchone()

            if row:
                if action == "move_up":
                    other = db.execute("""
                        SELECT id, sort_order
                        FROM home_slides
                        WHERE sort_order < ?
                        ORDER BY sort_order DESC, id DESC
                        LIMIT 1
                    """, (
                        row["sort_order"],
                    )).fetchone()
                else:
                    other = db.execute("""
                        SELECT id, sort_order
                        FROM home_slides
                        WHERE sort_order > ?
                        ORDER BY sort_order ASC, id ASC
                        LIMIT 1
                    """, (
                        row["sort_order"],
                    )).fetchone()

                if other:
                    db.execute("""
                        UPDATE home_slides
                        SET sort_order=?
                        WHERE id=?
                    """, (
                        other["sort_order"],
                        row["id"]
                    ))

                    db.execute("""
                        UPDATE home_slides
                        SET sort_order=?
                        WHERE id=?
                    """, (
                        row["sort_order"],
                        other["id"]
                    ))

                    db.commit()

        return redirect(url_for("home_slides"))

    rows = db.execute("""
        SELECT id, filename, original_name, sort_order, is_active, created_at
        FROM home_slides
        ORDER BY sort_order ASC, id ASC
    """).fetchall()

    return render_template(
        "home_slides.html",
        rows=rows
    )


# =========================
# Accounting (UI)
# =========================
@app.route("/accounting")
@login_required
@permission_required("accounting_home")
def accounting_home():
    return render_template("accounting_home.html")


def ensure_daily_accounting_schema(db):
    db.execute("PRAGMA foreign_keys = ON")

    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_header (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          es TEXT NOT NULL,
          day_date TEXT NOT NULL,
          cash_start REAL NOT NULL DEFAULT 0,
          cash_end REAL NOT NULL DEFAULT 0,
          cash_received REAL NOT NULL DEFAULT 0,
          mada REAL NOT NULL DEFAULT 0,
          bank_transfers REAL NOT NULL DEFAULT 0,
          total_in_enjaz REAL NOT NULL DEFAULT 0,
          notes TEXT,
          financial_approval_status TEXT NOT NULL DEFAULT '',
          financial_approval_updated_at TEXT,
          financial_approval_by TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT,
          UNIQUE(es, day_date)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_inputs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          input_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_tamara_emkan (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          pay_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_expenses_general (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          expense_type TEXT NOT NULL,
          payment_kind TEXT NOT NULL DEFAULT 'debt',
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_expenses_petty (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          expense_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_header_es_date
        ON daily_header(es, day_date)
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(daily_header)").fetchall()]

    if "total_in_enjaz" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN total_in_enjaz REAL NOT NULL DEFAULT 0")
    if "mada" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN mada REAL NOT NULL DEFAULT 0")
    if "bank_transfers" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN bank_transfers REAL NOT NULL DEFAULT 0")
    if "cash_received" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN cash_received REAL NOT NULL DEFAULT 0")
    if "financial_approval_status" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN financial_approval_status TEXT NOT NULL DEFAULT ''")
    if "financial_approval_updated_at" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN financial_approval_updated_at TEXT")
    if "financial_approval_by" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN financial_approval_by TEXT")

    # v2 ledger: existing days remain version 1, and are never rewritten by migration.
    if "ledger_version" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN ledger_version INTEGER NOT NULL DEFAULT 1")
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            header_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_daily_line_items_header ON daily_line_items(header_id)")

    gen_cols = [r[1] for r in db.execute("PRAGMA table_info(daily_expenses_general)").fetchall()]
    if "payment_kind" not in gen_cols:
        db.execute("ALTER TABLE daily_expenses_general ADD COLUMN payment_kind TEXT NOT NULL DEFAULT 'debt'")

    db.commit()


def _normalize_kind(kind: str) -> str:
    k = (kind or "").upper().strip()
    if k not in ("ES1", "ES2", "ES3"):
        return ""
    return k


def _require_accounting_kind_perm(kind: str):
    kind = _normalize_kind(kind)
    if not kind:
        return None, ("Not Found", 404)

    perm_key = f"accounting_{kind.lower()}"
    if not bool((session.get("perms") or {}).get(perm_key)):
        return None, (render_template("no_permission.html"), 403)

    return kind, None


@app.route("/accounting/<kind>")
@login_required
def accounting_es(kind):
    kind, err = _require_accounting_kind_perm(kind)
    if err:
        return err

    today = date.today().isoformat()
    return render_template("accounting_es.html", kind=kind, today=today)


def _to_float(v, default=0.0):
    try:
        if v is None:
            return float(default)
        s = str(v).strip().replace(",", "")
        if s == "":
            return float(default)
        value = float(s)
        # Reject non-finite financial input instead of persisting NaN/Infinity.
        return value if math.isfinite(value) else float(default)
    except (ValueError, TypeError, OverflowError):
        return float(default)


def _saudi_now_naive():
    """Current Saudi local time as a naive datetime for simple date-window checks."""
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)


def _accounting_edit_deadline(day_date: str):
    """
    Employee edit window: the accounting date stays editable until 48 hours
    after 00:00 at the beginning of that accounting day (Saudi local time).
    """
    try:
        d = datetime.strptime((day_date or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None
    return datetime.combine(d, datetime.min.time()) + timedelta(hours=48)


def _is_accounting_admin_editor(perms=None):
    """Master account and users explicitly granted late-accounting-edit permission."""
    if session.get("username") == MASTER_USERNAME:
        return True
    if perms is None:
        perms = session.get("perms") or {}
    return bool(perms.get("accounting_edit_after_48h"))


def _can_edit_accounting_day(day_date: str, perms: dict):
    deadline = _accounting_edit_deadline(day_date)
    if deadline is None:
        return False, False

    if _saudi_now_naive() < deadline:
        return True, False

    if _is_accounting_admin_editor(perms):
        return True, True

    return False, False


DAILY_LINE_CATEGORIES = (
    "inputs", "cash_returns", "debt", "payment", "tamara", "emkan",
    "petty", "mada", "bank_transfers", "cash_received",
)


def _daily_v2_sums(lines):
    sums = {key: 0.0 for key in DAILY_LINE_CATEGORIES}
    for line in lines:
        category = line["category"]
        if category in sums:
            amount = abs(_to_float(line["amount"]))
            sums[category] += amount
    return sums


def _daily_v2_totals(header, lines):
    sums = _daily_v2_sums(lines)
    cash_start = _to_float(header["cash_start"]) if header else 0.0
    cash_end = _to_float(header["cash_end"]) if header else 0.0
    cash_received = sums["cash_received"]
    mada = sums["mada"]
    bank_transfers = sums["bank_transfers"]
    tamara = sums["tamara"] + sums["emkan"]
    general = sums["debt"] - sums["payment"]
    total_in_enjaz = _to_float(header["total_in_enjaz"]) if header else 0.0
    total_overall = (cash_end + cash_received + tamara + mada + bank_transfers
                     + sums["inputs"] + general + sums["petty"] + sums["cash_returns"] - cash_start)
    return dict(inputs=sums["inputs"], cash_returns=sums["cash_returns"],
                tamara=tamara, general=general, petty=sums["petty"],
                cash_start=cash_start, cash_end=cash_end,
                cash_diff=cash_end-cash_start+cash_received,
                cash_received=cash_received, mada=mada, bank_transfers=bank_transfers,
                total_in_enjaz=total_in_enjaz, total_overall=total_overall,
                error=total_overall-total_in_enjaz)


def _may_complete_nov8_legacy_day(db, kind, day_date, header):
    """Allow the November 8 placeholder to become v2 only if empty.

    Never silently convert a historical day with financial movements or approval.
    """
    if day_date != "2026-11-08" or header is None:
        return False
    if kind not in ("ES1", "ES2", "ES3"):
        return False
    if int(header["ledger_version"] or 1) != 1:
        return False
    if (header["financial_approval_status"] or "").strip():
        return False
    for column in ("cash_end", "cash_received", "mada",
                   "bank_transfers", "total_in_enjaz", "tamara_emkan"):
        if column in header.keys() and abs(_to_float(header[column])) > 0.000001:
            return False
    for table in ("daily_inputs", "daily_tamara_emkan",
                  "daily_expenses_general", "daily_expenses_petty", "daily_line_items"):
        if db.execute(
            f"SELECT 1 FROM {table} WHERE header_id=? LIMIT 1", (header["id"],)
        ).fetchone():
            return False
    return True


@app.route("/accounting/<kind>/daily", methods=["GET", "POST"])
@login_required
def accounting_daily(kind):
    kind, err = _require_accounting_kind_perm(kind)
    if err:
        return err

    db = get_db()
    ensure_daily_accounting_schema(db)

    if request.method == "POST":
        day_date = (request.form.get("day_date") or "").strip() or date.today().isoformat()
    else:
        day_date = (request.args.get("date") or "").strip() or date.today().isoformat()

    if request.method == "POST" and request.form.get("action") == "open_date":
        return redirect(url_for("accounting_daily", kind=kind, date=day_date))

    perms = session.get("perms") or {}
    can_edit_day, has_after_48h_override = _can_edit_accounting_day(day_date, perms)
    edit_deadline = _accounting_edit_deadline(day_date)
    can_finance_approve = bool(perms.get("financial_admin_approval"))

    if request.method == "POST" and request.form.get("action") in ("finance_settled", "finance_rejected"):
        if not can_finance_approve:
            return render_template("no_permission.html"), 403

        approval_status = (
            "settled"
            if request.form.get("action") == "finance_settled"
            else "rejected"
        )

        approval_header = db.execute(
            "SELECT id FROM daily_header WHERE es=? AND day_date=?",
            (kind, day_date),
        ).fetchone()

        if not approval_header:
            flash("احفظ المحاسبة اليومية أولاً قبل اعتماد الإدارة المالية.", "warning")
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))

        db.execute(
            """
            UPDATE daily_header
               SET financial_approval_status=?,
                   financial_approval_updated_at=datetime('now'),
                   financial_approval_by=?
             WHERE id=?
            """,
            (
                approval_status,
                session.get("username") or "",
                approval_header["id"],
            ),
        )
        db.commit()

        if approval_status == "settled":
            flash("تم اعتماد الحالة: تمت التسوية بنجاح", "success")
        else:
            flash("تم اعتماد الحالة: مرفوض وتحت المعالجة", "danger")

        return redirect(url_for("accounting_daily", kind=kind, date=day_date))

    if request.method == "POST":
        if not can_edit_day:
            flash(
                "انتهت مهلة تعديل هذا التاريخ بعد مرور 48 ساعة. يلزم صلاحية الإدارة للتعديل.",
                "danger",
            )
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))

        if request.form.get("action") != "save":
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))

        legacy_header = db.execute(
            "SELECT * FROM daily_header WHERE es=? AND day_date=?",
            (kind, day_date),
        ).fetchone()
        if (legacy_header and int(legacy_header["ledger_version"] or 1) == 1
                and not _may_complete_nov8_legacy_day(db, kind, day_date, legacy_header)):
            flash("هذا السجل يحتوي على بيانات قديمة أو اعتماد، وهو محمي من التحويل التلقائي.", "warning")
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))

        cash_start = _to_float(request.form.get("cash_start"), 0)
        cash_end = _to_float(request.form.get("cash_end"), 0)
        total_in_enjaz = _to_float(request.form.get("total_in_enjaz"), 0)
        notes = (request.form.get("notes") or "").strip() or None
        categories = request.form.getlist("line_category")
        descriptions = request.form.getlist("line_description")
        amounts = request.form.getlist("line_amount")
        if not (len(categories) == len(descriptions) == len(amounts)):
            flash("بيانات صفوف العمليات غير مكتملة.", "danger")
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))
        if len(categories) > 500:
            flash("الحد الأقصى 500 عملية في اليوم.", "danger")
            return redirect(url_for("accounting_daily", kind=kind, date=day_date))

        line_items = []
        for category, description, amount_raw in zip(categories, descriptions, amounts):
            category = (category or "").strip()
            description = (description or "").strip()[:300]
            if category not in DAILY_LINE_CATEGORIES:
                flash("حالة العملية غير صحيحة.", "danger")
                return redirect(url_for("accounting_daily", kind=kind, date=day_date))
            if not (description or (amount_raw or "").strip()):
                continue
            try:
                amount = float(str(amount_raw or "0").replace(",", ""))
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError("Invalid amount")
            except ValueError:
                flash("المبلغ يجب أن يكون رقمًا موجبًا أو صفرًا.", "danger")
                return redirect(url_for("accounting_daily", kind=kind, date=day_date))
            line_items.append((category, description, amount))

        sums = _daily_v2_sums([{"category": c, "amount": v} for c, _, v in line_items])
        # Recheck the legacy record under the transaction before any write.
        with db:
            current_header = db.execute(
                "SELECT * FROM daily_header WHERE es=? AND day_date=?",
                (kind, day_date),
            ).fetchone()
            if (current_header and int(current_header["ledger_version"] or 1) == 1
                    and not _may_complete_nov8_legacy_day(db, kind, day_date, current_header)):
                flash("تم إيقاف الحفظ: السجل القديم يحتوي على بيانات محفوظة.", "danger")
                return redirect(url_for("accounting_daily", kind=kind, date=day_date))
            db.execute(
                "INSERT OR IGNORE INTO daily_header (es, day_date, created_at) VALUES (?, ?, datetime('now'))",
                (kind, day_date),
            )
            header_id = db.execute(
                "SELECT id FROM daily_header WHERE es=? AND day_date=?", (kind, day_date)
            ).fetchone()["id"]
            db.execute("""
                UPDATE daily_header
                   SET cash_start=?, cash_end=?, total_in_enjaz=?, notes=?,
                       cash_received=?, mada=?, bank_transfers=?, ledger_version=2,
                       updated_at=datetime('now')
                 WHERE id=?
            """, (cash_start, cash_end, total_in_enjaz, notes,
                  sums["cash_received"], sums["mada"], sums["bank_transfers"], header_id))
            db.execute("DELETE FROM daily_line_items WHERE header_id=?", (header_id,))
            db.executemany("""
                INSERT INTO daily_line_items (header_id, category, description, amount)
                VALUES (?, ?, ?, ?)
            """, [(header_id, c, d, v) for c, d, v in line_items])

        flash("تم حفظ المحاسبة اليومية بنجاح", "success")
        return redirect(url_for("accounting_daily", kind=kind, date=day_date))

    header = db.execute(
        "SELECT * FROM daily_header WHERE es=? AND day_date=?",
        (kind, day_date),
    ).fetchone()

    header_id = header["id"] if header else None
    inputs = []
    gen = []
    petty = []

    if header_id:
        inputs = db.execute(
            "SELECT input_type, amount FROM daily_inputs WHERE header_id=? ORDER BY id",
            (header_id,),
        ).fetchall()

        gen = db.execute(
            "SELECT expense_type, payment_kind, amount FROM daily_expenses_general WHERE header_id=? ORDER BY id",
            (header_id,),
        ).fetchall()

        petty = db.execute(
            "SELECT expense_type, amount FROM daily_expenses_petty WHERE header_id=? ORDER BY id",
            (header_id,),
        ).fetchall()

    is_v2 = not header or int(header["ledger_version"] or 1) == 2
    eligible_nov8 = (header is not None and
                     _may_complete_nov8_legacy_day(db, kind, day_date, header))
    legacy_read_only = not is_v2 and not eligible_nov8
    if legacy_read_only:
        can_edit_day = False  # Other historical values remain immutable.
    elif eligible_nov8:
        is_v2 = True  # Render new entry form without modifying the DB on GET.
    if header and is_v2:
        lines = db.execute("""
            SELECT category, description, amount FROM daily_line_items
             WHERE header_id=? ORDER BY id
        """, (header_id,)).fetchall()
    elif header:
        # Legacy presentation only: no write/migration of previous records.
        lines = ([{"category": "inputs", "description": r["input_type"], "amount": abs(r["amount"])} for r in inputs]
                 + [{"category": r["payment_kind"], "description": r["expense_type"], "amount": abs(r["amount"])} for r in gen]
                 + [{"category": "petty", "description": r["expense_type"], "amount": abs(r["amount"])} for r in petty])
        for cat in ("cash_received", "mada", "bank_transfers"):
            if _to_float(header[cat]):
                lines.append({"category": cat, "description": "", "amount": abs(_to_float(header[cat]))})
        old_payments = db.execute("SELECT pay_type, amount FROM daily_tamara_emkan WHERE header_id=?", (header_id,)).fetchall()
        lines.extend({"category": "tamara" if "tamara" in str(r["pay_type"]).lower() else "emkan",
                      "description": str(r["pay_type"]), "amount": abs(r["amount"])} for r in old_payments)
    else:
        lines = []

    def _sum(rows):
        return float(sum([_to_float(r["amount"], 0) for r in rows]))

    def _sum_by_kind(rows, kinds):
        return float(sum([
            _to_float(r["amount"], 0)
            for r in rows
            if r["payment_kind"] in kinds
        ]))

    totals = {
        "inputs": _sum(inputs),
        "tamara": _sum_by_kind(gen, ("tamara", "emkan")),
        "general": _sum_by_kind(gen, ("debt", "payment")),
        "petty": _sum(petty),
    }

    cash_start = _to_float(header["cash_start"], 0) if header else 0.0
    cash_end = _to_float(header["cash_end"], 0) if header else 0.0
    cash_received = _to_float(header["cash_received"], 0) if header else 0.0
    mada = _to_float(header["mada"], 0) if header else 0.0
    bank_transfers = _to_float(header["bank_transfers"], 0) if header else 0.0
    total_in_enjaz = _to_float(header["total_in_enjaz"], 0) if header else 0.0

    totals["cash_start"] = cash_start
    totals["cash_end"] = cash_end
    # فرق الصندوق = صندوق النهاية - صندوق البداية + الكاش المستلم
    totals["cash_diff"] = cash_end - cash_start + cash_received
    totals["cash_received"] = cash_received
    totals["mada"] = mada
    totals["bank_transfers"] = bank_transfers
    totals["total_in_enjaz"] = total_in_enjaz

    totals["total_overall"] = (
        cash_end
        + cash_received
        + totals["tamara"]
        + mada
        + bank_transfers
        + totals["inputs"]
        + totals["general"]
        + totals["petty"]
    ) - cash_start

    totals["error"] = totals["total_overall"] - total_in_enjaz
    totals["cash_returns"] = 0.0
    if is_v2:
        totals = _daily_v2_totals(header, lines)

    return render_template(
        "accounting_daily.html",
        kind=kind,
        day_date=day_date,
        header=header,
        inputs=inputs,
        gen=gen,
        petty=petty,
        lines=lines,
        totals=totals,
        legacy_read_only=legacy_read_only,
        can_edit_day=can_edit_day,
        has_after_48h_override=has_after_48h_override,
        edit_deadline=edit_deadline,
        can_finance_approve=can_finance_approve,
        can_legacy_edit=bool(legacy_read_only and _is_accounting_admin_editor(perms)),
    )


def _daily_legacy_totals(header, inputs, gen, petty):
    """Preserve v1 arithmetic: repayments were recorded as negative amounts."""
    def total(rows):
        return sum(_to_float(row["amount"]) for row in rows)
    cash_start = _to_float(header["cash_start"])
    cash_end = _to_float(header["cash_end"])
    cash_received = _to_float(header["cash_received"])
    mada = _to_float(header["mada"])
    bank_transfers = _to_float(header["bank_transfers"])
    enjaz = _to_float(header["total_in_enjaz"])
    tamara = sum(_to_float(row["amount"]) for row in gen
                 if row["payment_kind"] in ("tamara", "emkan"))
    general = sum(_to_float(row["amount"]) for row in gen
                  if row["payment_kind"] in ("debt", "payment"))
    inputs_sum = total(inputs)
    petty_sum = total(petty)
    overall = (cash_end + cash_received + tamara + mada + bank_transfers
               + inputs_sum + general + petty_sum - cash_start)
    return dict(cash_start=cash_start, cash_end=cash_end,
                cash_diff=cash_end-cash_start+cash_received,
                cash_received=cash_received, mada=mada,
                bank_transfers=bank_transfers, total_in_enjaz=enjaz,
                tamara=tamara, general=general, inputs=inputs_sum,
                petty=petty_sum, total_overall=overall, error=overall-enjaz)


def _ensure_daily_edit_history(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_edit_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          es TEXT NOT NULL,
          day_date TEXT NOT NULL,
          editor TEXT NOT NULL,
          edited_at TEXT NOT NULL DEFAULT (datetime('now')),
          previous_data TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_daily_edit_history_header "
               "ON daily_edit_history(header_id)")


def _legacy_form_number(raw):
    try:
        num = float(str(raw or '0').replace(',', ''))
    except (ValueError, TypeError):
        raise ValueError('invalid number')
    if not math.isfinite(num):
        raise ValueError('non-finite number')
    return num


@app.route("/accounting/<kind>/daily/legacy-edit", methods=["GET", "POST"])
@login_required
def accounting_daily_legacy_edit(kind):
    """Admin-only edit of v1 data in its original schema, without v2 conversion."""
    kind, err = _require_accounting_kind_perm(kind)
    if err:
        return err
    if not _is_accounting_admin_editor():
        return render_template("no_permission.html"), 403

    day_date = (request.args.get('date') or '').strip()
    try:
        datetime.strptime(day_date, '%Y-%m-%d')
    except ValueError:
        return 'Invalid accounting date', 400

    db = get_db()
    ensure_daily_accounting_schema(db)
    header = db.execute('SELECT * FROM daily_header WHERE es=? AND day_date=?',
                        (kind, day_date)).fetchone()
    if not header or int(header['ledger_version'] or 1) != 1:
        flash('هذا التاريخ ليس من سجلات النظام المحاسبي القديم.', 'warning')
        return redirect(url_for('accounting_daily', kind=kind, date=day_date))

    if request.method == 'POST':
        if request.form.get('action') != 'save':
            return redirect(url_for('accounting_daily_legacy_edit', kind=kind, date=day_date))
        if request.form.get('day_date') != day_date or request.form.get('confirm_legacy_edit') != 'yes':
            flash('يجب تأكيد تعديل السجل القديم والتأكد من التاريخ قبل الحفظ.', 'danger')
            return redirect(url_for('accounting_daily_legacy_edit', kind=kind, date=day_date))

        try:
            cash_start = _legacy_form_number(request.form.get('cash_start'))
            cash_end = _legacy_form_number(request.form.get('cash_end'))
            cash_received = _legacy_form_number(request.form.get('cash_received'))
            mada = _legacy_form_number(request.form.get('mada'))
            bank_transfers = _legacy_form_number(request.form.get('bank_transfers'))
            total_in_enjaz = _legacy_form_number(request.form.get('total_in_enjaz'))
            notes = (request.form.get('notes') or '').strip() or None

            input_types = request.form.getlist('input_type')
            input_amounts = request.form.getlist('input_amount')
            gen_types = request.form.getlist('gen_type')
            gen_kinds = request.form.getlist('gen_kind')
            gen_amounts = request.form.getlist('gen_amount')
            petty_types = request.form.getlist('petty_type')
            petty_amounts = request.form.getlist('petty_amount')
            if (len(input_types) != len(input_amounts) or
                    len(gen_types) != len(gen_kinds) or
                    len(gen_types) != len(gen_amounts) or
                    len(petty_types) != len(petty_amounts) or
                    max(len(input_types), len(gen_types), len(petty_types)) > 500):
                raise ValueError('mismatched rows')

            new_inputs = []
            for name, amount in zip(input_types, input_amounts):
                name = (name or '').strip()
                value = _legacy_form_number(amount)
                if name or value:
                    new_inputs.append((name or '(بدون عنوان)', value))

            new_gen = []
            for name, item_kind, amount in zip(gen_types, gen_kinds, gen_amounts):
                name = (name or '').strip()
                item_kind = (item_kind or 'debt').strip()
                if item_kind not in ('debt', 'payment', 'tamara', 'emkan'):
                    raise ValueError('invalid payment kind')
                value = _legacy_form_number(amount)
                value = -abs(value) if item_kind == 'payment' else abs(value)
                if name or value:
                    new_gen.append((name or '(بدون عنوان)', item_kind, value))

            new_petty = []
            for name, amount in zip(petty_types, petty_amounts):
                name = (name or '').strip()
                value = _legacy_form_number(amount)
                if name or value:
                    new_petty.append((name or '(بدون عنوان)', value))
        except ValueError:
            flash('تعذر الحفظ: تحقق من صحة المبالغ والصفوف ولا تترك قيمة غير رقمية.', 'danger')
            return redirect(url_for('accounting_daily_legacy_edit', kind=kind, date=day_date))

        with db:
            # Recheck while writing; ensure no simultaneous v2 conversion occurred.
            current = db.execute('SELECT * FROM daily_header WHERE id=? AND es=? AND day_date=?',
                                 (header['id'], kind, day_date)).fetchone()
            if not current or int(current['ledger_version'] or 1) != 1:
                flash('تغير نوع السجل؛ أُوقف الحفظ لحماية البيانات.', 'danger')
                return redirect(url_for('accounting_daily', kind=kind, date=day_date))
            _ensure_daily_edit_history(db)
            snapshot = {'header': dict(current)}
            for table in ('daily_inputs', 'daily_expenses_general',
                          'daily_expenses_petty', 'daily_tamara_emkan'):
                snapshot[table] = [dict(row) for row in db.execute(
                    f'SELECT * FROM {table} WHERE header_id=? ORDER BY id', (current['id'],)
                ).fetchall()]
            db.execute('''INSERT INTO daily_edit_history
                          (header_id,es,day_date,editor,previous_data)
                          VALUES (?,?,?,?,?)''',
                       (current['id'], kind, day_date,
                        session.get('username') or '', json.dumps(snapshot, ensure_ascii=False)))
            db.execute('''UPDATE daily_header
                          SET cash_start=?, cash_end=?, cash_received=?, mada=?,
                              bank_transfers=?, total_in_enjaz=?, notes=?,
                              financial_approval_status='',
                              financial_approval_updated_at=NULL,
                              financial_approval_by=NULL,
                              updated_at=datetime('now')
                          WHERE id=?''',
                       (cash_start, cash_end, cash_received, mada,
                        bank_transfers, total_in_enjaz, notes, current['id']))
            db.execute('DELETE FROM daily_inputs WHERE header_id=?', (current['id'],))
            db.execute('DELETE FROM daily_expenses_general WHERE header_id=?', (current['id'],))
            db.execute('DELETE FROM daily_expenses_petty WHERE header_id=?', (current['id'],))
            db.executemany('INSERT INTO daily_inputs(header_id,input_type,amount) VALUES (?,?,?)',
                           [(current['id'], name, value) for name, value in new_inputs])
            db.executemany('''INSERT INTO daily_expenses_general
                              (header_id,expense_type,payment_kind,amount) VALUES (?,?,?,?)''',
                           [(current['id'], name, category, value)
                            for name, category, value in new_gen])
            db.executemany('INSERT INTO daily_expenses_petty(header_id,expense_type,amount) VALUES (?,?,?)',
                           [(current['id'], name, value) for name, value in new_petty])

        flash('حُفظ تعديل الإدارة مع أرشفة البيانات السابقة. يلزم اعتماد الإدارة المالية مجددًا.', 'success')
        return redirect(url_for('accounting_daily', kind=kind, date=day_date))

    inputs = db.execute('SELECT * FROM daily_inputs WHERE header_id=? ORDER BY id',
                        (header['id'],)).fetchall()
    gen = db.execute('SELECT * FROM daily_expenses_general WHERE header_id=? ORDER BY id',
                     (header['id'],)).fetchall()
    petty = db.execute('SELECT * FROM daily_expenses_petty WHERE header_id=? ORDER BY id',
                       (header['id'],)).fetchall()
    return render_template('accounting_daily_legacy_admin.html', kind=kind,
                           day_date=day_date, header=header, inputs=inputs,
                           gen=gen, petty=petty,
                           totals=_daily_legacy_totals(header, inputs, gen, petty))


@app.route("/accounting/<kind>/movements", methods=["GET"])
@login_required
def accounting_movements(kind):
    kind, err = _require_accounting_kind_perm(kind)
    if err:
        return err

    db = get_db()
    ensure_daily_accounting_schema(db)

    date_from = (request.args.get("from") or "").strip() or date.today().isoformat()
    date_to = (request.args.get("to") or "").strip() or date.today().isoformat()
    approval_filter = (request.args.get("approval") or "").strip()

    if approval_filter not in ("settled", "rejected"):
        approval_filter = ""

    sql = """
        SELECT id,
               day_date,
               cash_start,
               cash_end,
               cash_received,
               ledger_version,
               mada,
               bank_transfers,
               total_in_enjaz,
               financial_approval_status
          FROM daily_header
         WHERE es=?
           AND day_date BETWEEN ? AND ?
    """
    params = [kind, date_from, date_to]

    if approval_filter:
        sql += " AND financial_approval_status=?"
        params.append(approval_filter)

    sql += " ORDER BY day_date DESC"

    headers = db.execute(sql, params).fetchall()

    rows = []

    overall_inputs = 0.0
    overall_tamara = 0.0
    overall_general = 0.0
    overall_petty = 0.0
    overall_cash_start = 0.0
    overall_cash_end = 0.0
    overall_cash_diff = 0.0
    overall_cash_received = 0.0
    overall_cash_returns = 0.0
    overall_mada = 0.0
    overall_bank_transfers = 0.0
    overall_total_in_enjaz = 0.0

    for h in headers:
        hid = h["id"]

        total_inputs = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_inputs WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        # تمارا/إمكان: ندعم البيانات القديمة من daily_tamara_emkan
        # والبيانات الحالية المخزنة في daily_expenses_general.
        old_tamara = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_tamara_emkan WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        current_tamara = _to_float(db.execute(
            '''
            SELECT COALESCE(SUM(amount),0) AS s
            FROM daily_expenses_general
            WHERE header_id=?
              AND payment_kind IN ('tamara', 'emkan')
            ''',
            (hid,)
        ).fetchone()["s"], 0)

        total_tamara = old_tamara + current_tamara

        total_general = _to_float(db.execute(
            '''
            SELECT COALESCE(SUM(amount),0) AS s
            FROM daily_expenses_general
            WHERE header_id=?
              AND payment_kind IN ('debt', 'payment')
            ''',
            (hid,)
        ).fetchone()["s"], 0)

        total_petty = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_expenses_petty WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        is_v2 = int(h["ledger_version"] or 1) == 2
        if is_v2:
            day_lines = db.execute(
                "SELECT category, amount FROM daily_line_items WHERE header_id=?", (hid,)
            ).fetchall()
            v2_totals = _daily_v2_totals(h, day_lines)

        cash_start = _to_float(h["cash_start"], 0)
        cash_end = _to_float(h["cash_end"], 0)
        cash_received = _to_float(h["cash_received"], 0)
        cash_diff = cash_end - cash_start + cash_received
        mada = _to_float(h["mada"], 0)
        bank_transfers = _to_float(h["bank_transfers"], 0)
        total_in_enjaz = _to_float(h["total_in_enjaz"], 0)

        total_overall = (
            cash_end
            + cash_received
            + total_tamara
            + mada
            + bank_transfers
            + total_inputs
            + total_general
            + total_petty
        ) - cash_start

        cash_returns = 0.0
        if is_v2:
            total_inputs = v2_totals["inputs"]
            total_tamara = v2_totals["tamara"]
            total_general = v2_totals["general"]
            total_petty = v2_totals["petty"]
            cash_received = v2_totals["cash_received"]
            mada = v2_totals["mada"]
            bank_transfers = v2_totals["bank_transfers"]
            cash_diff = v2_totals["cash_diff"]
            total_overall = v2_totals["total_overall"]
            cash_returns = v2_totals["cash_returns"]

        err_val = total_overall - total_in_enjaz

        rows.append({
            "day_date": h["day_date"],
            "cash_start": cash_start,
            "cash_end": cash_end,
            "cash_diff": cash_diff,
            "cash_received": cash_received,
            "cash_returns": cash_returns,
            "tamara": total_tamara,
            "mada": mada,
            "bank_transfers": bank_transfers,
            "total_inputs": total_inputs,
            "total_general": total_general,
            "total_petty": total_petty,
            "total_in_enjaz": total_in_enjaz,
            "total_overall": total_overall,
            "error": err_val,
            "financial_approval_status": (h["financial_approval_status"] or "").strip(),
        })

        overall_inputs += total_inputs
        overall_tamara += total_tamara
        overall_general += total_general
        overall_petty += total_petty
        overall_cash_start += cash_start
        overall_cash_end += cash_end
        overall_cash_diff += cash_diff
        overall_cash_received += cash_received
        overall_cash_returns += cash_returns
        overall_mada += mada
        overall_bank_transfers += bank_transfers
        overall_total_in_enjaz += total_in_enjaz

    overall_total_overall = (
        overall_cash_end
        + overall_cash_received
        + overall_tamara
        + overall_mada
        + overall_bank_transfers
        + overall_inputs
        + overall_general
        + overall_petty
        + overall_cash_returns
    ) - overall_cash_start

    overall_error = overall_total_overall - overall_total_in_enjaz

    overall = {
        "inputs": overall_inputs,
        "tamara": overall_tamara,
        "general": overall_general,
        "petty": overall_petty,
        "cash_start": overall_cash_start,
        "cash_end": overall_cash_end,
        "cash_diff": overall_cash_diff,
        "cash_received": overall_cash_received,
        "cash_returns": overall_cash_returns,
        "mada": overall_mada,
        "bank_transfers": overall_bank_transfers,
        "total_in_enjaz": overall_total_in_enjaz,
        "total_overall": overall_total_overall,
        "error": overall_error,
    }

    return render_template(
        "accounting_movements.html",
        kind=kind,
        date_from=date_from,
        date_to=date_to,
        approval_filter=approval_filter,
        rows=rows,
        overall=overall,
    )
@app.route("/accounting/commitments", methods=["GET", "POST"])
@login_required
@permission_required("financial_commitments")
def accounting_commitments_home():
    db = get_db()
    ensure_financial_commitments_schema(db)

    action = request.form.get("action")

    GS_TAB = "tasks_commitment"
    GS_HEADERS = ["id", "party", "amount", "created_at"]

    if request.method == "POST":
        if action == "add":
            party = (request.form.get("party") or "").strip()
            amount = _to_float(request.form.get("amount"), 0)

            if party:
                cur = db.execute(
                    "INSERT INTO financial_commitments (party, amount) VALUES (?, ?)",
                    (party, amount),
                )
                db.commit()

                cid = cur.lastrowid
                create_or_update_commitment_task(db, cid)
                # --- Google Sheets upsert ---
                try:
                    row = db.execute(
                        "SELECT id, party, amount, created_at FROM financial_commitments WHERE id=?",
                        (cid,),
                    ).fetchone()

                    if row:
                        ws, err = open_ws(GS_TAB)
                        if not err:
                            # ✅ تأكد من الهيدرز أولاً
                            ws_ensure_headers(ws, GS_HEADERS)

                            # ✅ upsert
                            ok = ws_upsert(
                                ws,
                                GS_HEADERS,
                                [row["id"], row["party"], row["amount"], row["created_at"]],
                                row["id"],
                            )

                            # ws_upsert لازم يرجع True/False بعد تعديل الدالة
                            if ok is False:
                                flash("⚠️ فشل تصدير Google Sheets (commitments/add) راجع logs", "warning")
                except Exception as e:
                    print("Google Sheets export (tasks_commitment/add) error:", e)

        elif action == "update":
            cid = int(request.form.get("id") or 0)
            party = (request.form.get("party") or "").strip()
            amount = _to_float(request.form.get("amount"), 0)

            db.execute(
                "UPDATE financial_commitments SET party=?, amount=?, updated_at=datetime('now') WHERE id=?",
                (party, amount, cid),
            )
            db.commit()

            create_or_update_commitment_task(db, cid)

            # --- Google Sheets upsert ---
            try:
                row = db.execute(
                    "SELECT id, party, amount, created_at FROM financial_commitments WHERE id=?",
                    (cid,),
                ).fetchone()

                if row:
                    ws, err = open_ws(GS_TAB)
                    if not err:
                        # ✅ تأكد من الهيدرز أولاً
                        ws_ensure_headers(ws, GS_HEADERS)

                        # ✅ upsert
                        ok = ws_upsert(
                            ws,
                            GS_HEADERS,
                            [row["id"], row["party"], row["amount"], row["created_at"]],
                            row["id"],
                        )

                        if ok is False:
                            flash("⚠️ فشل تصدير Google Sheets (commitments/update) راجع logs", "warning")
            except Exception as e:
                print("Google Sheets export (tasks_commitment/update) error:", e)

        elif action == "delete":
            cid = int(request.form.get("id") or 0)

            delete_commitment_task(db, cid)

            # --- Google Sheets delete ---
            try:
                ws, err = open_ws(GS_TAB)
                if not err:
                    deleted = ws_delete_by_id(ws, cid)  # ترجع True/False عندك

                    # (اختياري) تنبيه لو ما انحذف
                    if deleted is False:
                        flash("⚠️ تعذر حذف الصف من Google Sheets (commitments/delete) راجع logs", "warning")
            except Exception as e:
                print("Google Sheets export (tasks_commitment/delete) error:", e)

            db.execute("DELETE FROM financial_commitments WHERE id=?", (cid,))
            db.commit()

        return redirect(url_for("accounting_commitments_home"))

    rows = db.execute("SELECT * FROM financial_commitments ORDER BY id DESC").fetchall()
    total = float(sum([_to_float(r["amount"], 0) for r in rows]))

    return render_template("accounting_commitments_home.html", rows=rows, total=total)

from urllib.parse import quote


@app.route("/accounting/debts", methods=["GET", "POST"])
@login_required
@permission_required("accounting_home")
def accounting_debts():
    return accounting_debts_branch("ALL")

def normalize_sa_mobile_for_whatsapp(mobile):
    mobile = str(mobile or "").strip()
    mobile = mobile.replace(" ", "").replace("-", "").replace("+", "")

    if mobile.startswith("0"):
        mobile = "966" + mobile[1:]

    if mobile.startswith("5"):
        mobile = "966" + mobile

    return mobile


def build_debt_whatsapp_message(customer_name, remaining_amount, breakdown=None):
    """Prepared draft for a staff-reviewed WhatsApp message; never send automatically."""
    lines = [
        "مؤسسة آل عِشان لقطع غيار السيارات",
        "",
        f"عميلنا العزيز {customer_name}،",
        f"نود إشعاركم بوجود مبلغ مستحق عليكم قدره {remaining_amount:.2f} ريال.",
    ]
    if breakdown and len(breakdown) > 1:
        lines += ["", "تفاصيل المبالغ حسب الفروع:"]
        for code, amount in breakdown:
            lines.append(f"{code}: {amount:.2f} ريال")
    lines += [
        "",
        "نأمل منكم السداد أو التحويل على حسابنا البنكي في مصرف الراجحي.",
        "رقم الحساب: 257000010006080319523",
        "رقم الآيبان: SA8180000257608010319523",
        "وفي حال التحويل، نرجو إبلاغنا وتزويدنا بإشعار السداد لتحديث حسابكم.",
        "",
        "شاكرين لكم حسن تعاونكم.",
        "مؤسسة آل عِشان لقطع غيار السيارات",
    ]
    return "\n".join(lines)


def has_debt_permission(es):
    perms = session.get("perms") or {}

    if es == "ALL":
        return (
            perms.get("debts_es1")
            or perms.get("debts_es2")
            or perms.get("debts_es3")
        )

    perm_map = {
        "ES1": "debts_es1",
        "ES2": "debts_es2",
        "ES3": "debts_es3",
    }

    return perms.get(perm_map.get(es))


@app.route("/accounting/debts/<es>/whatsapp/<int:row_id>")
@login_required
def accounting_debts_whatsapp(es,row_id):
    # Keep old bookmarks working: go straight to the project's WhatsApp inbox.
    # The destination validates debt permissions, consent and the fixed sender.
    return redirect(url_for("debt_whatsapp_direct", es=es, debt_id=row_id))


@app.route("/accounting/debts/<es>", methods=["GET", "POST"])
@login_required
def accounting_debts_branch(es):
    db = get_db()
    ensure_debts_ledger_schema(db)

    es = (es or "").strip().upper()
    if es not in ["ALL", "ES1", "ES2", "ES3"]:
        flash("الفرع غير صحيح", "danger")
        return redirect(url_for("accounting_home"))

    if not has_debt_permission(es):
        flash("ليس لديك صلاحية للوصول إلى هذه الصفحة", "danger")
        return redirect(url_for("home"))

    DEBT_CUSTOMERS['setup']()  # safe lazy migration, never merges existing debts automatically
    debt_delete_token()  # only for the master administrator
    action = (request.form.get("action") or "").strip()

    if request.method == "POST":
        invoice_date = (request.form.get("invoice_date") or "").strip()
        customer_name = (request.form.get("customer_name") or "").strip()
        mobile = (request.form.get("mobile") or "").strip()
        address = (request.form.get("address") or "").strip()

        branch_es = (request.form.get("branch_es") or "").strip().upper()
        if branch_es not in ["ES1", "ES2", "ES3"]:
            branch_es = "ES1" if es == "ALL" else es

        if not has_debt_permission(branch_es):
            flash("ليس لديك صلاحية لهذا الفرع", "danger")
            return redirect(url_for("accounting_debts") if es == "ALL" else url_for("accounting_debts_branch", es=es))

        invoice_total = _to_float(request.form.get("invoice_total"))
        paid_amount = _to_float(request.form.get("paid_amount"))
        remaining_amount = invoice_total - paid_amount

        if action == "add":
            if not customer_name:
                flash("اسم العميل مطلوب", "danger")
            elif not (invoice_total > 0 and 0 <= paid_amount <= invoice_total):
                flash("تحقق من مبلغ الفاتورة والمبلغ المسدد", "danger")
            else:
                # Search across *all* branches; require a decision before saving.
                DEBT_CUSTOMERS['setup']()
                choices = DEBT_CUSTOMERS['display_matches'](db,customer_name,mobile)
                # Guard against double-booking the *same debt*, not just duplicate names.
                exact_duplicates = []
                for old in db.execute('''SELECT id,invoice_date,customer_name,mobile,invoice_total,paid_amount
                    FROM debts_ledger WHERE es=? AND ABS(COALESCE(invoice_total,0)-?) < 0.005
                    AND ABS(COALESCE(paid_amount,0)-?) < 0.005''',
                    (branch_es,invoice_total,paid_amount)).fetchall():
                    if (old['invoice_date'] or '') != invoice_date:
                        continue
                    if ((normalized_phone(old['mobile']) and normalized_phone(old['mobile']) == normalized_phone(mobile))
                        or normalized_name(old['customer_name']) == normalized_name(customer_name)):
                        exact_duplicates.append(old)
                linked_id = (request.form.get('linked_customer_id') or '').strip()
                force_new = request.form.get('force_new_customer') == 'yes'
                reason = (request.form.get('override_reason') or '').strip()
                selected = None
                invoice_repeat_ok = (session.get('username') == MASTER_USERNAME
                   and request.form.get('repeat_invoice_approved') == 'yes'
                   and len(reason) >= 8)
                if exact_duplicates and not invoice_repeat_ok:
                    return render_template('debt_duplicate_review.html',
                      matches=choices, exact_duplicates=exact_duplicates,
                      form_data=dict(request.form), es=es,
                      is_admin=session.get('username') == MASTER_USERNAME)
                if linked_id:
                    try:
                        chosen_id = int(linked_id)
                    except ValueError:
                        chosen_id = -1
                    if chosen_id in [c['id'] for c in choices]:
                        selected = db.execute('''SELECT * FROM debt_customers
                          WHERE id=? AND merged_into_customer_id IS NULL''',(chosen_id,)).fetchone()
                    if not selected:
                        flash('اختيار العميل الموجود غير صالح. راجع نتائج البحث مجددًا.','danger')
                        return redirect(url_for('accounting_debts_branch',es=es))
                    # Identity protection: do not reuse WhatsApp consent for a different mobile.
                    old_phone = normalized_phone(selected['mobile'])
                    new_phone = normalized_phone(mobile)
                    if old_phone and new_phone and old_phone != new_phone:
                        flash('جوال العميل مختلف عن الملف الموجود؛ لا يمكن الربط قبل تصحيح البيانات من الإدارة.','danger')
                        return redirect(url_for('accounting_debts_branch',es=es))
                    name_on_file = normalized_name(selected['name'])
                    new_name = normalized_name(customer_name)
                    if (session.get('username') != MASTER_USERNAME and name_on_file != new_name
                        and name_on_file not in new_name and new_name not in name_on_file):
                        flash('هذا الجوال مرتبط باسم مختلف؛ يلزم مراجعة الإدارة قبل ربط الملفين.','danger')
                        return redirect(url_for('accounting_debts_branch',es=es))
                    if not mobile and selected['mobile']:
                        mobile = selected['mobile']
                    if not address and selected['address']:
                        address = selected['address']
                elif choices and not force_new:
                    return render_template('debt_duplicate_review.html',
                      matches=choices, form_data=dict(request.form), es=es,
                      is_admin=session.get('username')==MASTER_USERNAME)
                elif choices and force_new and (session.get('username')!=MASTER_USERNAME or len(reason)<8):
                    flash('إنشاء عميل منفصل رغم وجود تشابه يحتاج موافقة الإدارة وسببًا من 8 أحرف على الأقل.','danger')
                    return render_template('debt_duplicate_review.html',
                      matches=choices, form_data=dict(request.form), es=es,
                      is_admin=session.get('username')==MASTER_USERNAME)
                with db:
                    if selected:
                        customer_id = int(selected['id'])
                        if not normalized_phone(selected['mobile']) and normalized_phone(mobile):
                            db.execute('''UPDATE debt_customers SET mobile=?,normalized_phone=?,
                              updated_at=datetime('now') WHERE id=?''',
                              (mobile,normalized_phone(mobile),customer_id))
                            # A newly assigned phone is not covered by prior consent.
                            db.execute('''UPDATE debt_customer_policy SET policy='pending',
                              consent_at=NULL,consent_source=NULL,updated_at=datetime('now')
                              WHERE customer_id=?''',(customer_id,))
                    else:
                        customer_id = DEBT_CUSTOMERS['create_customer'](db,customer_name,mobile,address)
                        if choices and force_new:
                            db.execute('''INSERT INTO debt_customer_override_audit
                              (new_customer_id,reason,performed_by) VALUES (?,?,?)''',
                              (customer_id,reason[:500],session.get('username') or ''))
                    cursor = db.execute('''INSERT INTO debts_ledger
                      (es,invoice_no,invoice_date,customer_name,mobile,address,
                       invoice_total,paid_amount,remaining_amount,customer_id)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                      (branch_es,'',invoice_date,customer_name,mobile,address,
                       invoice_total,paid_amount,remaining_amount,customer_id))
                    if exact_duplicates:
                        db.execute('''INSERT INTO debt_duplicate_invoice_override_audit
                          (debt_id,similar_debt_id,reason,performed_by)
                          VALUES (?,?,?,?)''',
                          (cursor.lastrowid,exact_duplicates[0]['id'],reason[:500],session.get('username') or ''))
                flash('تم حفظ الدين ' + ('ضمن ملف العميل الموجود.' if selected else 'وإنشاء ملف العميل.'),'success')

        elif action == "update":
            row_id = (request.form.get("id") or "").strip()
            original = db.execute("SELECT es,mobile,customer_id FROM debts_ledger WHERE id=?",(row_id,)).fetchone() if row_id else None
            if not original or not has_debt_permission(original["es"]):
                return render_template("no_permission.html"), 403

            if not row_id:
                flash("معرّف السجل غير موجود", "danger")
            elif not customer_name:
                flash("اسم العميل مطلوب", "danger")
            elif db.execute("""SELECT COUNT(*) FROM debts_ledger
                     WHERE customer_id=(SELECT customer_id FROM debts_ledger WHERE id=?)""",
                    (row_id,)).fetchone()[0] > 1 and (
                    normalized_name(customer_name) != normalized_name(db.execute(
                    'SELECT customer_name FROM debts_ledger WHERE id=?',(row_id,)).fetchone()[0]) or
                    normalized_phone(mobile) != normalized_phone(db.execute(
                    'SELECT mobile FROM debts_ledger WHERE id=?',(row_id,)).fetchone()[0])):
                flash("هذا العميل مرتبط بديون أخرى؛ لا يمكن تغيير الاسم أو الجوال من فاتورة واحدة. راجع الإدارة.", "danger")
            else:
                db.execute("""
                    UPDATE debts_ledger
                    SET es = ?,
                        invoice_date = ?,
                        customer_name = ?,
                        mobile = ?,
                        address = ?,
                        invoice_total = ?,
                        paid_amount = ?,
                        remaining_amount = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (
                    branch_es,
                    invoice_date,
                    customer_name,
                    mobile,
                    address,
                    invoice_total,
                    paid_amount,
                    remaining_amount,
                    row_id
                ))
                if original and normalized_phone(original['mobile']) != normalized_phone(mobile):
                    # A consent given to one mobile must never transfer to another.
                    db.execute("""UPDATE debt_customer_policy SET policy='pending',
                      consent_at=NULL,consent_source=NULL,updated_at=datetime('now')
                      WHERE customer_id=?""",(original['customer_id'],))
                cust = db.execute("SELECT customer_id FROM debts_ledger WHERE id=?",(row_id,)).fetchone()
                if cust and cust['customer_id']:
                    db.execute("""UPDATE debt_customers
                       SET name=?,normalized_name=?,mobile=?,normalized_phone=?,address=?,updated_at=datetime('now')
                       WHERE id=? AND (SELECT COUNT(*) FROM debts_ledger WHERE customer_id=?)=1""",
                       (customer_name,normalized_name(customer_name),mobile,normalized_phone(mobile),address,
                        cust['customer_id'],cust['customer_id']))
                db.commit()
                flash("تم تعديل السجل", "success")

        elif action == "mark_sent":
            flash("تم إلغاء تأكيد الإرسال اليدوي. راجع سجل واتساب الفعلي في ملف التحصيل.","warning")

        elif action == "delete":
            # Delete exactly one invoice, never a customer profile or a group.
            # Preserve its original values in an administrator-only archive.
            if session.get("username") != MASTER_USERNAME:
                return render_template("no_permission.html"), 403
            if not debt_delete_token_valid():
                flash("انتهت صلاحية تأكيد الحذف. حدّث الصفحة وأعد المحاولة.", "danger")
            else:
                try:
                    row_id = int(request.form.get("id") or "")
                except (ValueError, TypeError):
                    row_id = 0
                original = db.execute("SELECT * FROM debts_ledger WHERE id=?", (row_id,)).fetchone() if row_id > 0 else None
                if not original:
                    flash("سجل الدين غير موجود أو حُذف سابقًا.", "warning")
                elif (original["es"] != branch_es or (es != "ALL" and original["es"] != es)):
                    return render_template("no_permission.html"), 403
                elif request.form.get("confirm_delete") != f"DELETE:{row_id}":
                    flash("لم يتم تأكيد حذف الدين المحدد.", "danger")
                else:
                    ensure_debt_deleted_archive(db)
                    with db:
                        db.execute("""INSERT INTO debt_deleted_archive
                          (debt_id,branch_es,customer_id,snapshot_json,deleted_by)
                          VALUES (?,?,?,?,?)""",
                          (row_id,original["es"],original["customer_id"],
                           json.dumps(dict(original),ensure_ascii=False),session["username"]))
                        deleted = db.execute("DELETE FROM debts_ledger WHERE id=? AND es=?",
                                             (row_id,original["es"])).rowcount
                        if deleted != 1:
                            raise sqlite3.IntegrityError("Debt deleted concurrently: rollback archive")
                    flash(f"نُقل الدين رقم {row_id} إلى محذوفات الديون، ويمكن للإدارة استعادته.", "success")

        if es == "ALL":
            return redirect(url_for("accounting_debts"))

        return redirect(url_for("accounting_debts_branch", es=es))

    q = (request.args.get("q") or "").strip()
    if es == "ALL":
        rows = db.execute("SELECT * FROM debts_ledger ORDER BY id DESC").fetchall()
    else:
        rows = db.execute("SELECT * FROM debts_ledger WHERE es=? ORDER BY id DESC", (es,)).fetchall()
    rows = [r for r in rows if has_debt_permission(r["es"])]
    if q:
        # Match on the canonical profile or on ANY historical invoice alias.
        # Once a customer matches, return ALL permitted invoice rows for that
        # customer, not just the single old alias/invoice that matched.
        name_query = normalized_name(q)
        phone_query = ''.join(ch for ch in q if ch.isdigit())
        profiles = db.execute('''SELECT id,normalized_name,normalized_phone,name,mobile
            FROM debt_customers WHERE merged_into_customer_id IS NULL''').fetchall()
        matching_ids = set()
        for c in profiles:
            if ((name_query and name_query in (c['normalized_name'] or ''))
                    or (phone_query and phone_query in (c['normalized_phone'] or ''))
                    or q in (c['mobile'] or '')):
                matching_ids.add(int(c['id']))
        for r in rows:
            if ((name_query and name_query in normalized_name(r['customer_name']))
                    or (phone_query and phone_query in ''.join(ch for ch in (r['mobile'] or '') if ch.isdigit()))):
                if r['customer_id']:
                    matching_ids.add(int(r['customer_id']))
        rows = [r for r in rows if int(r['customer_id'] or 0) in matching_ids]

    canonical_phones = {
        int(c["id"]): c["mobile"]
        for c in db.execute("SELECT id,mobile FROM debt_customers WHERE merged_into_customer_id IS NULL").fetchall()
    }

    # Customer policies apply across every branch. Display each management
    # decision INSTEAD of the Send Message button when sending is not approved.
    # Query once (rather than one lookup per invoice/row).
    policy_by_customer = {
        int(p['customer_id']): p
        for p in db.execute('SELECT customer_id,policy,consent_at,consent_source,dispute,pause_until FROM debt_customer_policy').fetchall()
    }
    debt_message_decisions = {
        int(r['id']): debt_message_decision(
            r, policy_by_customer.get(int(r['customer_id'] or 0))
        )
        for r in rows
    }
    # The sender and staff reply permissions must be ready as well.
    if any(d['can_send'] for d in debt_message_decisions.values()):
        ensure_whatsapp_schema(db)
        collection_sender = db.execute('''SELECT n.id FROM debt_collection_settings s
            JOIN wa_numbers n ON n.id=s.wa_number_id
            WHERE s.id=1 AND n.is_active=1''').fetchone()
        reply_numbers = set(_wa_allowed_number_ids(db, require_reply=True))
        for r in rows:
            d = debt_message_decisions[int(r['id'])]
            if not d['can_send']:
                continue
            if not collection_sender:
                debt_message_decisions[int(r['id'])] = {
                    'can_send': False, 'tone': 'warning',
                    'label': 'رقم واتساب التحصيل غير محدد'
                }
            elif int(collection_sender['id']) not in reply_numbers:
                debt_message_decisions[int(r['id'])] = {
                    'can_send': False, 'tone': 'warning',
                    'label': 'لا توجد صلاحية لرقم واتساب التحصيل'
                }
            elif not _wa_normalize_destination(canonical_phones.get(int(r['customer_id'] or 0))):
                debt_message_decisions[int(r['id'])] = {
                    'can_send': False, 'tone': 'warning',
                    'label': 'رقم جوال العميل غير صالح'
                }

    # Group debt lines by their canonical customer profile. The ledger retains
    # every historical invoice, but a merged customer appears only once in the
    # customer overview. Do not rewrite legacy names, amounts or dates.
    customer_profiles = {
        int(c["id"]): c for c in db.execute(
            "SELECT id,name,mobile FROM debt_customers WHERE merged_into_customer_id IS NULL"
        ).fetchall()
    }
    merge_counts = {
        int(c["target_customer_id"]): int(c["count"])
        for c in db.execute("""SELECT target_customer_id,COUNT(*) AS count
            FROM debt_customer_merge_audit GROUP BY target_customer_id""").fetchall()
    }
    grouped = {}
    for item in rows:
        cid = int(item["customer_id"] or 0)
        # Migrations link every invoice to a profile. A legacy orphan, if any,
        # is displayed independently and never combined with unrelated debt.
        key = cid if cid else -int(item["id"])
        if key not in grouped:
            profile = customer_profiles.get(cid)
            grouped[key] = {
                "customer_id": cid,
                "name": profile["name"] if profile else item["customer_name"],
                "mobile": profile["mobile"] if profile else item["mobile"],
                "items": [], "invoice": 0.0, "paid": 0.0,
                "remaining": 0.0, "branches": set(),
                "merge_count": merge_counts.get(cid, 0),
            }
        group = grouped[key]
        group["items"].append(item)
        group["invoice"] += float(item["invoice_total"] or 0)
        group["paid"] += float(item["paid_amount"] or 0)
        group["remaining"] += float(item["remaining_amount"] or 0)
        group["branches"].add(item["es"])
    customer_groups = list(grouped.values())
    for group in customer_groups:
        group["branches"] = ", ".join(sorted(group["branches"]))
        group["debt_count"] = len(group["items"])
        group["representative"] = next(
            (item for item in group["items"] if float(item["remaining_amount"] or 0) > 0),
            group["items"][0],
        )
        group["decision"] = debt_message_decisions.get(int(group["representative"]["id"]))

    total_invoice = sum((r["invoice_total"] or 0) for r in rows)
    total_paid = sum((r["paid_amount"] or 0) for r in rows)
    total_remaining = sum((r["remaining_amount"] or 0) for r in rows)

    edit_row = None
    edit_id = (request.args.get("edit_id") or "").strip()

    if edit_id:
        edit_row = db.execute("""
            SELECT *
            FROM debts_ledger
            WHERE id = ?
        """, (edit_id,)).fetchone()
        if edit_row and not has_debt_permission(edit_row["es"]):
            return render_template("no_permission.html"), 403

    return render_template(
        "accounting_debts_branch.html",
        es=es,
        rows=rows,
        edit_row=edit_row,
        debt_message_decisions=debt_message_decisions,
        customer_groups=customer_groups,
        total_invoice=total_invoice,
        total_paid=total_paid,
        total_remaining=total_remaining,
    )


@app.route("/accounting/debts/trash", methods=["GET"])
@login_required
def accounting_debts_trash():
    if session.get("username") != MASTER_USERNAME:
        return render_template("no_permission.html"), 403
    db = get_db()
    ensure_debts_ledger_schema(db)
    ensure_debt_deleted_archive(db)
    debt_delete_token()
    deleted_rows = db.execute("""SELECT * FROM debt_deleted_archive
          WHERE restored_at IS NULL ORDER BY id DESC LIMIT 500""").fetchall()
    archived = []
    for entry in deleted_rows:
        try:
            snapshot = json.loads(entry["snapshot_json"])
            if not isinstance(snapshot, dict):
                snapshot = {}
        except (ValueError, TypeError):
            snapshot = {}
        archived.append({"archive": entry, "debt": snapshot})
    return render_template("accounting_debts_trash.html", archived=archived)


@app.route("/accounting/debts/trash/<int:archive_id>/restore", methods=["POST"])
@login_required
def accounting_debts_restore(archive_id):
    if session.get("username") != MASTER_USERNAME:
        return render_template("no_permission.html"), 403
    if not debt_delete_token_valid():
        flash("انتهت صلاحية تأكيد الاستعادة. حدّث الصفحة وأعد المحاولة.", "danger")
        return redirect(url_for("accounting_debts_trash"))
    db = get_db()
    ensure_debts_ledger_schema(db)
    ensure_debt_deleted_archive(db)
    entry = db.execute("""SELECT * FROM debt_deleted_archive
        WHERE id=? AND restored_at IS NULL""", (archive_id,)).fetchone()
    if not entry:
        flash("هذا الدين غير موجود في المحذوفات أو تم استعادته سابقًا.", "warning")
        return redirect(url_for("accounting_debts_trash"))
    try:
        snapshot = json.loads(entry["snapshot_json"])
    except (TypeError, ValueError):
        snapshot = None
    if not isinstance(snapshot, dict) or not snapshot.get("id") or snapshot.get("es") != entry["branch_es"]:
        flash("تعذر استعادة هذا السجل لأن بيانات النسخة الاحتياطية غير مكتملة.", "danger")
        return redirect(url_for("accounting_debts_trash"))
    if int(snapshot["id"]) != int(entry["debt_id"]):
        flash("معرف الدين في الأرشيف غير متطابق.", "danger")
        return redirect(url_for("accounting_debts_trash"))
    cols = [row[1] for row in db.execute("PRAGMA table_info(debts_ledger)").fetchall()]
    restored_cols = [col for col in cols if col in snapshot]
    if not {"id", "es", "customer_name", "invoice_total", "paid_amount", "remaining_amount"}.issubset(restored_cols):
        flash("لا يمكن استعادة السجل؛ بعض أعمدة الدين الأساسية غير متاحة.", "danger")
        return redirect(url_for("accounting_debts_trash"))
    if db.execute("SELECT 1 FROM debts_ledger WHERE id=?", (entry["debt_id"],)).fetchone():
        flash("لا يمكن الاستعادة لأن رقم الدين مستخدم بالفعل في الدفتر.", "danger")
        return redirect(url_for("accounting_debts_trash"))
    quoted_cols = ",".join('"' + col.replace('"','""') + '"' for col in restored_cols)
    placeholders = ",".join("?" for _ in restored_cols)
    with db:
        db.execute(f"INSERT INTO debts_ledger ({quoted_cols}) VALUES ({placeholders})",
                   [snapshot[col] for col in restored_cols])
        db.execute("""UPDATE debt_deleted_archive
              SET restored_by=?,restored_at=datetime('now') WHERE id=? AND restored_at IS NULL""",
                   (session["username"], archive_id))
    flash(f"تمت استعادة الدين رقم {entry['debt_id']} بجميع مبالغه الأصلية.", "success")
    return redirect(url_for("accounting_debts_trash"))


@app.route("/accounting/debts/<es>/detail/<int:row_id>")
@login_required
def accounting_debts_detail(es, row_id):
    db = get_db()
    ensure_debts_ledger_schema(db)

    es = (es or "").strip().upper()
    if es not in ["ALL", "ES1", "ES2", "ES3"]:
        flash("الفرع غير صحيح", "danger")
        return redirect(url_for("accounting_home"))

    row = db.execute("""
        SELECT *
        FROM debts_ledger
        WHERE id = ?
    """, (row_id,)).fetchone()

    if not row:
        flash("السجل غير موجود", "danger")
        return redirect(url_for("accounting_debts"))

    if not has_debt_permission(row["es"]):
        flash("ليس لديك صلاحية للوصول إلى هذه الصفحة", "danger")
        return redirect(url_for("home"))

    debt_delete_token()
    return render_template("accounting_debts_detail.html", row=row, es=es)


@app.route("/accounting/debts/<es>/export")
@login_required
def accounting_debts_export(es):
    db = get_db()
    ensure_debts_ledger_schema(db)

    es = (es or "").strip().upper()
    if es not in ["ALL", "ES1", "ES2", "ES3"]:
        flash("الفرع غير صحيح", "danger")
        return redirect(url_for("accounting_home"))

    if not has_debt_permission(es):
        flash("ليس لديك صلاحية للوصول إلى هذه الصفحة", "danger")
        return redirect(url_for("home"))

    if es == "ALL":
        rows = db.execute("""
            SELECT es, invoice_date, customer_name, mobile, address,
                   invoice_total, paid_amount, remaining_amount,
                   last_sent_at, created_at, updated_at
            FROM debts_ledger
            ORDER BY id DESC
        """).fetchall()
    else:
        rows = db.execute("""
            SELECT es, invoice_date, customer_name, mobile, address,
                   invoice_total, paid_amount, remaining_amount,
                   last_sent_at, created_at, updated_at
            FROM debts_ledger
            WHERE es = ?
            ORDER BY id DESC
        """, (es,)).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "دفتر الديون" if es == "ALL" else f"دفتر الديون {es}"

    ws.append([
        "الفرع",
        "تاريخ الفاتورة",
        "اسم العميل",
        "رقم الجوال",
        "العنوان",
        "إجمالي الفاتورة",
        "المبلغ المسدد",
        "المبلغ المتبقي",
        "آخر تاريخ للإرسال",
        "تاريخ الإنشاء",
        "آخر تحديث",
    ])

    for r in rows:
        ws.append([
            r["es"],
            r["invoice_date"],
            r["customer_name"],
            r["mobile"],
            r["address"],
            r["invoice_total"],
            r["paid_amount"],
            r["remaining_amount"],
            r["last_sent_at"],
            r["created_at"],
            r["updated_at"],
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    file_name = "debts_all.xlsx" if es == "ALL" else f"debts_{es}.xlsx"

    return send_file(
        output,
        as_attachment=True,
        download_name=file_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.route("/suggestions", methods=["GET", "POST"])
@login_required
def suggestions_page():
    db = get_db()

    db.execute("""
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        admin_note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(suggestions)").fetchall()]
    if "admin_note" not in cols:
        db.execute("ALTER TABLE suggestions ADD COLUMN admin_note TEXT DEFAULT ''")

    db.commit()

    is_admin = (session.get("username") == "adm-es")

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        text = (request.form.get("text") or "").strip()

        if action == "add" and text:
            db.execute(
                "INSERT INTO suggestions (text, status, admin_note) VALUES (?, 'pending', '')",
                (text,)
            )
            db.commit()
            flash("تمت إضافة المقترح", "success")

        elif action == "edit":
            sid = int(request.form.get("id") or 0)
            if sid and text:
                db.execute(
                    "UPDATE suggestions SET text=? WHERE id=?",
                    (text, sid)
                )
                db.commit()
                flash("تم تعديل المقترح", "success")

        elif action == "delete":
            sid = int(request.form.get("id") or 0)
            if sid:
                db.execute("DELETE FROM suggestions WHERE id=?", (sid,))
                db.commit()
                flash("تم حذف المقترح", "info")

        elif action == "approve" and is_admin:
            sid = int(request.form.get("id") or 0)
            admin_note = (request.form.get("admin_note") or "").strip()
            if sid:
                db.execute(
                    "UPDATE suggestions SET status='approved', admin_note=? WHERE id=?",
                    (admin_note, sid)
                )
                db.commit()
                flash("تمت الموافقة على المقترح", "success")

        elif action == "reject" and is_admin:
            sid = int(request.form.get("id") or 0)
            admin_note = (request.form.get("admin_note") or "").strip()
            if sid:
                db.execute(
                    "UPDATE suggestions SET status='rejected', admin_note=? WHERE id=?",
                    (admin_note, sid)
                )
                db.commit()
                flash("تم رفض المقترح", "warning")

        elif action == "save_note" and is_admin:
            sid = int(request.form.get("id") or 0)
            admin_note = (request.form.get("admin_note") or "").strip()
            if sid:
                db.execute(
                    "UPDATE suggestions SET admin_note=? WHERE id=?",
                    (admin_note, sid)
                )
                db.commit()
                flash("تم حفظ ملاحظات الإدارة", "success")

        return redirect(url_for("suggestions_page"))

    rows = db.execute("""
        SELECT *
        FROM suggestions
        ORDER BY id DESC
    """).fetchall()

    return render_template(
        "suggestions.html",
        rows=rows,
        is_admin=is_admin
    )

def normalize_url(url):
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


@app.route("/important-links", methods=["GET", "POST"])
@login_required
def important_links():
    db = get_db()
    ensure_important_links_schema(db)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        link_id = (request.form.get("id") or "").strip()
        title = (request.form.get("title") or "").strip()
        url = normalize_url(request.form.get("url"))
        category = (request.form.get("category") or "").strip()

        if action in ("add", "update"):
            if not title:
                flash("وصف الموقع مطلوب", "warning")
                return redirect(url_for("important_links"))
            if not url:
                flash("الرابط مطلوب", "warning")
                return redirect(url_for("important_links"))

        if action == "add":
            max_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS m FROM important_links"
            ).fetchone()["m"]

            db.execute("""
                INSERT INTO important_links
                (title, url, category, sort_order, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (title, url, category, max_order + 1))
            db.commit()
            flash("تمت إضافة الرابط", "success")

        elif action == "update" and link_id:
            db.execute("""
                UPDATE important_links
                SET title=?, url=?, category=?, updated_at=datetime('now')
                WHERE id=?
            """, (title, url, category, link_id))
            db.commit()
            flash("تم تعديل الرابط", "success")

        elif action == "delete" and link_id:
            db.execute("DELETE FROM important_links WHERE id=?", (link_id,))
            db.commit()
            flash("تم حذف الرابط", "warning")

        elif action == "toggle_pin" and link_id:
            row = db.execute(
                "SELECT is_pinned FROM important_links WHERE id=?",
                (link_id,)
            ).fetchone()
            if row:
                new_val = 0 if row["is_pinned"] else 1
                db.execute(
                    "UPDATE important_links SET is_pinned=?, updated_at=datetime('now') WHERE id=?",
                    (new_val, link_id)
                )
                db.commit()

        elif action in ("move_up", "move_down") and link_id:
            row = db.execute(
                "SELECT id, sort_order FROM important_links WHERE id=?",
                (link_id,)
            ).fetchone()

            if row:
                if action == "move_up":
                    other = db.execute("""
                        SELECT id, sort_order
                        FROM important_links
                        WHERE sort_order < ?
                        ORDER BY sort_order DESC
                        LIMIT 1
                    """, (row["sort_order"],)).fetchone()
                else:
                    other = db.execute("""
                        SELECT id, sort_order
                        FROM important_links
                        WHERE sort_order > ?
                        ORDER BY sort_order ASC
                        LIMIT 1
                    """, (row["sort_order"],)).fetchone()

                if other:
                    db.execute("UPDATE important_links SET sort_order=? WHERE id=?",
                               (other["sort_order"], row["id"]))
                    db.execute("UPDATE important_links SET sort_order=? WHERE id=?",
                               (row["sort_order"], other["id"]))
                    db.commit()

        return redirect(url_for("important_links"))

    q = (request.args.get("q") or "").strip()
    category_filter = (request.args.get("category") or "").strip()
    edit_id = (request.args.get("edit_id") or "").strip()

    where = []
    params = []

    if q:
        where.append("title LIKE ?")
        params.append(f"%{q}%")

    if category_filter:
        where.append("category = ?")
        params.append(category_filter)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    rows = db.execute(f"""
        SELECT *
        FROM important_links
        {where_sql}
        ORDER BY is_pinned DESC, sort_order ASC, id DESC
    """, params).fetchall()

    edit_row = None
    if edit_id:
        edit_row = db.execute(
            "SELECT * FROM important_links WHERE id=?",
            (edit_id,)
        ).fetchone()

    categories = db.execute("""
        SELECT DISTINCT category
        FROM important_links
        WHERE category IS NOT NULL AND category != ''
        ORDER BY category ASC
    """).fetchall()

    total_count = db.execute("SELECT COUNT(*) AS c FROM important_links").fetchone()["c"]

    return render_template(
        "important_links.html",
        rows=rows,
        edit_row=edit_row,
        categories=categories,
        total_count=total_count,
        q=q,
        category_filter=category_filter
    )

# =========================
# Monthly Commission
# =========================
def ensure_monthly_commission_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS monthly_commission_settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          commission_rate REAL NOT NULL DEFAULT 0.008,
          fixed_deduction REAL NOT NULL DEFAULT 200,
          employees_count INTEGER NOT NULL DEFAULT 6,
          updated_at TEXT
        )
    """)
    db.execute("""
        INSERT OR IGNORE INTO monthly_commission_settings
            (id, commission_rate, fixed_deduction, employees_count, updated_at)
        VALUES (1, 0.008, 200, 6, datetime('now'))
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS monthly_commissions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          month TEXT NOT NULL UNIQUE,
          sales_before_tax REAL NOT NULL DEFAULT 0,
          total_commission REAL NOT NULL DEFAULT 0,
          employee_commission REAL NOT NULL DEFAULT 0,
          rate_snapshot REAL,
          deduction_snapshot REAL,
          employees_snapshot INTEGER,
          created_at TEXT,
          updated_at TEXT
        )
    """)

    cols = [r[1] for r in db.execute("PRAGMA table_info(monthly_commissions)").fetchall()]

    def addcol(name, decl):
        if name not in cols:
            db.execute(f"ALTER TABLE monthly_commissions ADD COLUMN {name} {decl}")

    addcol("updated_at", "TEXT")
    addcol("rate_snapshot", "REAL")
    addcol("deduction_snapshot", "REAL")
    addcol("employees_snapshot", "INTEGER")

    db.execute("""
        UPDATE monthly_commissions
           SET updated_at = COALESCE(updated_at, created_at)
         WHERE updated_at IS NULL
    """)
    db.commit()


def get_monthly_commission_settings(db) -> dict:
    ensure_monthly_commission_schema(db)
    row = db.execute("""
        SELECT commission_rate, fixed_deduction, employees_count
          FROM monthly_commission_settings
         WHERE id=1
    """).fetchone()

    if not row:
        return {"commission_rate": 0.008, "fixed_deduction": 200.0, "employees_count": 6}

    return {
        "commission_rate": float(row[0]),
        "fixed_deduction": float(row[1]),
        "employees_count": int(row[2]),
    }


def calculate_monthly_commission(sales: float, settings: dict):
    rate = float(settings.get("commission_rate", 0.008))
    deduction = float(settings.get("fixed_deduction", 200))
    emp_count = int(settings.get("employees_count", 6)) or 1

    total_commission = sales * rate
    employee_commission = (total_commission - deduction) / emp_count
    return total_commission, employee_commission


@app.route("/accounting/monthly-commission", methods=["GET", "POST"])
@login_required
@permission_required("monthly_commission")
def monthly_commission():
    db = get_db()
    settings = get_monthly_commission_settings(db)

    GS_TAB = "commission"
    GS_HEADERS = ["id", "month", "sales", "total_commission", "employee_commission", "saved_at"]

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_settings":
            try:
                rate = float((request.form.get("rate") or "0").strip())
                base_deduction = float((request.form.get("base_deduction") or "0").strip())
                employees_count = int((request.form.get("employees_count") or "1").strip())
                if employees_count < 1:
                    employees_count = 1
            except Exception:
                flash("قيم الإعدادات غير صحيحة", "warning")
                return redirect(url_for("monthly_commission"))

            db.execute("""
                UPDATE monthly_commission_settings
                   SET commission_rate=?,
                       fixed_deduction=?,
                       employees_count=?,
                       updated_at=datetime('now')
                 WHERE id=1
            """, (rate, base_deduction, employees_count))
            db.commit()
            flash("تم حفظ إعدادات المعادلات ✅", "success")
            return redirect(url_for("monthly_commission"))

        month = (request.form.get("month") or "").strip()
        sales_raw = (request.form.get("sales") or "").strip()

        if not month:
            flash("اختر الشهر", "warning")
            return redirect(url_for("monthly_commission"))

        try:
            sales = float(sales_raw)
        except ValueError:
            flash("مبلغ المبيعات غير صحيح", "warning")
            return redirect(url_for("monthly_commission"))

        total_commission, employee_commission = calculate_monthly_commission(sales, settings)

        existing = db.execute("SELECT id FROM monthly_commissions WHERE month=?", (month,)).fetchone()

        if existing:
            db.execute("""
                UPDATE monthly_commissions
                   SET sales_before_tax=?,
                       total_commission=?,
                       employee_commission=?,
                       rate_snapshot=?,
                       deduction_snapshot=?,
                       employees_snapshot=?,
                       updated_at=datetime('now')
                 WHERE month=?
            """, (
                sales,
                total_commission,
                employee_commission,
                settings["commission_rate"],
                settings["fixed_deduction"],
                settings["employees_count"],
                month,
            ))
            db.commit()
            row_id = int(existing["id"])
            flash("تم تعديل العمولة", "success")
        else:
            cur = db.execute("""
                INSERT INTO monthly_commissions
                    (month, sales_before_tax, total_commission, employee_commission,
                     rate_snapshot, deduction_snapshot, employees_snapshot,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (
                month,
                sales,
                total_commission,
                employee_commission,
                settings["commission_rate"],
                settings["fixed_deduction"],
                settings["employees_count"],
            ))
            db.commit()
            row_id = int(cur.lastrowid)
            flash("تم حفظ العمولة", "success")

        # تصدير إلى Google Sheets
        try:
            r = db.execute("""
                SELECT id, month, sales_before_tax, total_commission, employee_commission, updated_at
                  FROM monthly_commissions
                 WHERE id=?
            """, (row_id,)).fetchone()

            if r:
                ws, err = open_ws(GS_TAB)
                if not err:
                    ok = ws_upsert(ws, GS_HEADERS, [
                        r["id"],
                        r["month"],
                        r["sales_before_tax"],
                        r["total_commission"],
                        r["employee_commission"],
                        r["updated_at"],
                    ], r["id"])
                    if not ok:
                        flash("⚠️ فشل تصدير Google Sheets (commission) راجع logs", "warning")
        except Exception as e:
            print("Google Sheets export (commission) error:", e)

        return redirect(url_for("monthly_commission"))

    rows = db.execute("""
        SELECT month, sales_before_tax, total_commission, employee_commission,
               rate_snapshot, deduction_snapshot, employees_snapshot,
               created_at, updated_at
          FROM monthly_commissions
         ORDER BY month DESC
    """).fetchall()

    return render_template(
        "accounting_monthly_commission.html",
        rows=rows,
        settings=settings,
        rate=settings["commission_rate"],
        base_deduction=settings["fixed_deduction"],
        employees_count=settings["employees_count"],
    )


# =========================
# Records
# =========================
@app.route("/records")
@login_required
@permission_required("records_home")
def records_home():
    return render_template("records_home.html")


def table_name(kind: str):
    kind = kind.upper()
    if kind not in ("ES1", "ES2", "ES3"):
        raise ValueError("Invalid kind")
    return f"records_{kind.lower()}"


def require_records_perm(kind: str) -> bool:
    key = f"records_{kind.lower()}"
    return bool((session.get("perms") or {}).get(key))


@app.route("/records/<kind>")
@login_required
def records_list(kind):
    kind = kind.upper()
    if not require_records_perm(kind):
        return render_template("no_permission.html"), 403

    t = table_name(kind)
    db = get_db()
    rows = db.execute(
        f"SELECT id, record_type, record_number, expiry_date FROM {t} ORDER BY id ASC"
    ).fetchall()

    data = []
    for r in rows:
        expiry = parse_date(r["expiry_date"]) if r["expiry_date"] else None
        data.append({
            "id": r["id"],
            "record_type": r["record_type"],
            "record_number": r["record_number"],
            "expiry_date": expiry,
        })
    return render_template("records_list.html", kind=kind, rows=data)


@app.route("/records/<kind>/new", methods=["GET", "POST"])
@login_required
def record_new(kind):
    kind = kind.upper()
    if not require_records_perm(kind):
        return render_template("no_permission.html"), 403

    t = table_name(kind)

    # ===== GET =====
    if request.method == "GET":
        return render_template("record_form.html", kind=kind, mode="new", row=None)

    # ===== POST =====
    record_type = request.form.get("record_type", "").strip()
    record_number = request.form.get("record_number", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip()

    if not record_number:
        flash("رقم السجل مطلوب", "warning")
        return render_template("record_form.html", kind=kind, mode="new", row=None)

    db = get_db()
    db.execute(
        f"INSERT INTO {t} (record_type, record_number, expiry_date) VALUES (?, ?, ?)",
        (record_type, record_number, expiry_date or None),
    )
    db.commit()
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # --- Google Sheets export ---
    try:
        ws, err = open_ws(f"records_{kind.lower()}")
        if not err:
            headers = ["id", "record_type", "record_number", "expiry_date"]
            ws_ensure_headers(ws, headers)

            ok = ws_upsert(
                ws,
                headers,
                [new_id, record_type, record_number, expiry_date or ""],
                new_id
            )
            if ok is False:
                print("records export failed (new):", kind, new_id)
                flash("⚠️ فشل تصدير Google Sheets (records/new) راجع logs", "warning")
    except Exception as e:
        print("Google Sheets export (records/new) error:", e)

    flash("تمت الإضافة بنجاح", "success")
    return redirect(url_for("records_list", kind=kind))


@app.route("/records/<kind>/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def record_edit(kind, rid: int):
    kind = kind.upper()
    if not require_records_perm(kind):
        return render_template("no_permission.html"), 403

    t = table_name(kind)
    db = get_db()
    row = db.execute(
        f"SELECT id, record_type, record_number, expiry_date FROM {t} WHERE id=?",
        (rid,),
    ).fetchone()

    if row is None:
        flash("السجل غير موجود", "danger")
        return redirect(url_for("records_list", kind=kind))

    if request.method == "POST":
        record_type = request.form.get("record_type", "").strip()
        record_number = request.form.get("record_number", "").strip()
        expiry_date = request.form.get("expiry_date", "").strip()

        if not record_number:
            flash("رقم السجل مطلوب", "warning")
            return render_template("record_form.html", kind=kind, mode="edit", row=row)

        db.execute(
            f"UPDATE {t} SET record_type=?, record_number=?, expiry_date=? WHERE id=?",
            (record_type, record_number, expiry_date or None, rid),
        )
        db.commit()

        ws, err = open_ws(f"records_{kind.lower()}")
        if not err:
            headers = ["id", "record_type", "record_number", "expiry_date"]

            ws_ensure_headers(ws, headers)

            ok = ws_upsert(
                ws,
                headers,
                [rid, record_type, record_number, expiry_date or ""],
                rid
            )

            if ok is False:
                print("records export failed (edit):", kind, rid)
                flash(
                    "⚠️ فشل تصدير Google Sheets (records/edit) راجع logs",
                    "warning"
                )

        flash("تم التعديل بنجاح", "success")
        return redirect(url_for("records_list", kind=kind))

    return render_template("record_form.html", kind=kind, mode="edit", row=row)


@app.route("/records/<kind>/<int:rid>/delete", methods=["POST"])
@login_required
def record_delete(kind, rid: int):
    kind = kind.upper()
    if not require_records_perm(kind):
        return render_template("no_permission.html"), 403

    t = table_name(kind)
    db = get_db()
    db.execute(f"DELETE FROM {t} WHERE id=?", (rid,))
    db.commit()

    ws, err = open_ws(f"records_{kind.lower()}")
    if not err:
        ws_delete_by_id(ws, rid)

    flash("تم الحذف", "info")
    return redirect(url_for("records_list", kind=kind))

# =========================
# Employees
# =========================

def ensure_employees_schema(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()]

    if "nationality" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN nationality TEXT")
    if "mobile" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN mobile TEXT")
    if "bank_account" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN bank_account TEXT")
    if "debts" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN debts REAL NOT NULL DEFAULT 0")
    if "contract_no" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN contract_no TEXT")
    if "contract_expiry" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN contract_expiry TEXT")

    db.commit()


@app.route("/employees")
@login_required
@permission_required("employees")
def employees_list():
    db = get_db()
    ensure_employees_schema(db)

    rows = db.execute("""
        SELECT id, name_ar, name_en, nationality, mobile,
               contract_no, contract_expiry,
               passport_no, passport_expiry,
               iqama_no, iqama_expiry,
               insurance_name, insurance_expiry
        FROM employees
        ORDER BY id ASC
    """).fetchall()

    data = []
    for r in rows:
        data.append({
            "id": r["id"],
            "name_ar": r["name_ar"],
            "name_en": r["name_en"],
            "nationality": r["nationality"],
            "mobile": r["mobile"],
            "contract_no": r["contract_no"],
            "contract_expiry": parse_date(r["contract_expiry"]) if r["contract_expiry"] else None,
            "passport_no": r["passport_no"],
            "passport_expiry": parse_date(r["passport_expiry"]) if r["passport_expiry"] else None,
            "iqama_no": r["iqama_no"],
            "iqama_expiry": parse_date(r["iqama_expiry"]) if r["iqama_expiry"] else None,
            "insurance_name": r["insurance_name"],
            "insurance_expiry": parse_date(r["insurance_expiry"]) if r["insurance_expiry"] else None,
        })

    return render_template("employees_list.html", rows=data)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
@permission_required("employees")
def employee_new():
    db = get_db()
    ensure_employees_schema(db)

    if request.method == "POST":
        payload = {
            "name_ar": request.form.get("name_ar", "").strip(),
            "name_en": request.form.get("name_en", "").strip(),
            "nationality": request.form.get("nationality", "").strip(),
            "mobile": request.form.get("mobile", "").strip(),
            "passport_no": request.form.get("passport_no", "").strip(),
            "passport_expiry": request.form.get("passport_expiry", "").strip() or None,
            "iqama_no": request.form.get("iqama_no", "").strip(),
            "iqama_expiry": request.form.get("iqama_expiry", "").strip() or None,
            "insurance_name": request.form.get("insurance_name", "").strip(),
            "insurance_expiry": request.form.get("insurance_expiry", "").strip() or None,
            "contract_no": request.form.get("contract_no", "").strip(),
            "contract_expiry": request.form.get("contract_expiry", "").strip() or None,
            "basic_salary": _to_float(request.form.get("basic_salary"), 0),
            "commission": _to_float(request.form.get("commission"), 0),
            "bank_account": request.form.get("bank_account", "").strip(),
            "debts": _to_float(request.form.get("debts"), 0),
        }

        if not payload["name_ar"]:
            flash("اسم الموظف بالعربي مطلوب", "warning")
            return render_template("employee_form.html", mode="new", row=None)

        db.execute("""
            INSERT INTO employees (
              name_ar, name_en, nationality, mobile,
              passport_no, passport_expiry,
              iqama_no, iqama_expiry,
              insurance_name, insurance_expiry,
              contract_no, contract_expiry,
              basic_salary, commission, bank_account, debts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload["name_ar"],
            payload["name_en"],
            payload["nationality"],
            payload["mobile"],
            payload["passport_no"],
            payload["passport_expiry"],
            payload["iqama_no"],
            payload["iqama_expiry"],
            payload["insurance_name"],
            payload["insurance_expiry"],
            payload["contract_no"],
            payload["contract_expiry"],
            payload["basic_salary"],
            payload["commission"],
            payload["bank_account"],
            payload["debts"],
        ))

        db.commit()

        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        ws, err = open_ws("employees")
        if not err:
            # نضيف حقول العقد في نهاية Google Sheets للحفاظ على ترتيب الأعمدة القديمة.
            headers = [
                "id", "name_ar", "name_en", "nationality", "mobile",
                "passport_no", "passport_expiry",
                "iqama_no", "iqama_expiry",
                "insurance_name", "insurance_expiry",
                "basic_salary", "commission", "bank_account", "debts",
                "contract_no", "contract_expiry"
            ]

            ws_ensure_headers(ws, headers)

            ok = ws_upsert(ws, headers, [
                new_id,
                payload["name_ar"],
                payload["name_en"],
                payload["nationality"],
                payload["mobile"],
                payload["passport_no"],
                payload["passport_expiry"] or "",
                payload["iqama_no"],
                payload["iqama_expiry"] or "",
                payload["insurance_name"],
                payload["insurance_expiry"] or "",
                payload["basic_salary"] or "",
                payload["commission"] or "",
                payload["bank_account"],
                payload["debts"],
                payload["contract_no"],
                payload["contract_expiry"] or "",
            ], new_id)

            if not ok:
                flash("⚠️ فشل تصدير Google Sheets (employees/new) راجع logs", "warning")

        flash("تمت إضافة الموظف", "success")
        return redirect(url_for("employees_list"))

    return render_template("employee_form.html", mode="new", row=None)


@app.route("/employees/<int:eid>")
@login_required
@permission_required("employees")
def employee_detail(eid: int):
    db = get_db()
    ensure_employees_schema(db)

    r = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()

    if r is None:
        flash("الموظف غير موجود", "danger")
        return redirect(url_for("employees_list"))

    row = dict(r)

    for k in ("passport_expiry", "iqama_expiry", "insurance_expiry", "contract_expiry"):
        row[k] = parse_date(row[k]) if row.get(k) else None

    return render_template("employee_detail.html", row=row)


@app.route("/employees/<int:eid>/edit", methods=["GET", "POST"])
@login_required
@permission_required("employees")
def employee_edit(eid: int):
    db = get_db()
    ensure_employees_schema(db)

    row_db = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()

    if row_db is None:
        flash("الموظف غير موجود", "danger")
        return redirect(url_for("employees_list"))

    row = dict(row_db)

    for k in [
        "name_ar", "name_en", "nationality", "mobile",
        "passport_no", "passport_expiry",
        "iqama_no", "iqama_expiry",
        "insurance_name", "insurance_expiry",
        "contract_no", "contract_expiry",
        "bank_account"
    ]:
        row[k] = row.get(k) or ""

    row["basic_salary"] = row.get("basic_salary") or 0
    row["commission"] = row.get("commission") or 0
    row["debts"] = row.get("debts") or 0

    if request.method == "POST":
        payload = {
            "name_ar": request.form.get("name_ar", "").strip(),
            "name_en": request.form.get("name_en", "").strip(),
            "nationality": request.form.get("nationality", "").strip(),
            "mobile": request.form.get("mobile", "").strip(),
            "passport_no": request.form.get("passport_no", "").strip(),
            "passport_expiry": request.form.get("passport_expiry", "").strip() or None,
            "iqama_no": request.form.get("iqama_no", "").strip(),
            "iqama_expiry": request.form.get("iqama_expiry", "").strip() or None,
            "insurance_name": request.form.get("insurance_name", "").strip(),
            "insurance_expiry": request.form.get("insurance_expiry", "").strip() or None,
            "contract_no": request.form.get("contract_no", "").strip(),
            "contract_expiry": request.form.get("contract_expiry", "").strip() or None,
            "basic_salary": _to_float(request.form.get("basic_salary"), 0),
            "commission": _to_float(request.form.get("commission"), 0),
            "bank_account": request.form.get("bank_account", "").strip(),
            "debts": _to_float(request.form.get("debts"), 0),
        }

        if not payload["name_ar"]:
            flash("اسم الموظف بالعربي مطلوب", "warning")
            return render_template("employee_form.html", mode="edit", row=row)

        db.execute("""
            UPDATE employees SET
                name_ar=?,
                name_en=?,
                nationality=?,
                mobile=?,
                passport_no=?,
                passport_expiry=?,
                iqama_no=?,
                iqama_expiry=?,
                insurance_name=?,
                insurance_expiry=?,
                contract_no=?,
                contract_expiry=?,
                basic_salary=?,
                commission=?,
                bank_account=?,
                debts=?
            WHERE id=?
        """, (
            payload["name_ar"],
            payload["name_en"],
            payload["nationality"],
            payload["mobile"],
            payload["passport_no"],
            payload["passport_expiry"],
            payload["iqama_no"],
            payload["iqama_expiry"],
            payload["insurance_name"],
            payload["insurance_expiry"],
            payload["contract_no"],
            payload["contract_expiry"],
            payload["basic_salary"],
            payload["commission"],
            payload["bank_account"],
            payload["debts"],
            eid,
        ))

        db.commit()

        try:
            ws, err = open_ws("employees")

            if not err:
                # نضيف حقول العقد في نهاية Google Sheets للحفاظ على ترتيب الأعمدة القديمة.
                headers = [
                    "id", "name_ar", "name_en", "nationality", "mobile",
                    "passport_no", "passport_expiry",
                    "iqama_no", "iqama_expiry",
                    "insurance_name", "insurance_expiry",
                    "basic_salary", "commission", "bank_account", "debts",
                    "contract_no", "contract_expiry"
                ]

                ws_ensure_headers(ws, headers)

                ok = ws_upsert(ws, headers, [
                    eid,
                    payload["name_ar"],
                    payload["name_en"],
                    payload["nationality"],
                    payload["mobile"],
                    payload["passport_no"],
                    payload["passport_expiry"] or "",
                    payload["iqama_no"],
                    payload["iqama_expiry"] or "",
                    payload["insurance_name"],
                    payload["insurance_expiry"] or "",
                    payload["basic_salary"] or "",
                    payload["commission"] or "",
                    payload["bank_account"],
                    payload["debts"],
                    payload["contract_no"],
                    payload["contract_expiry"] or "",
                ], eid)

                if not ok:
                    flash("⚠️ تم حفظ التعديل، لكن فشل تصدير Google Sheets", "warning")

        except Exception as e:
            print("employees/edit google sheets error:", e)
            flash("⚠️ تم حفظ التعديل، لكن فشل تصدير Google Sheets", "warning")

        flash("تم تعديل بيانات الموظف", "success")
        return redirect(url_for("employee_detail", eid=eid))

    return render_template("employee_form.html", mode="edit", row=row)


@app.route("/employees/<int:eid>/delete", methods=["POST"])
@login_required
@permission_required("employees")
def employee_delete(eid: int):
    db = get_db()
    ensure_employees_schema(db)

    row = db.execute("SELECT id FROM employees WHERE id=?", (eid,)).fetchone()

    if not row:
        flash("الموظف غير موجود", "warning")
        return redirect(url_for("employees_list"))

    try:
        db.execute("DELETE FROM employees WHERE id=?", (eid,))
        db.commit()
    except Exception as e:
        db.rollback()
        print("employee_delete db error:", e)
        flash("تعذر حذف الموظف من قاعدة البيانات", "danger")
        return redirect(url_for("employees_list"))

    try:
        ws, err = open_ws("employees")
        if not err:
            ws_delete_by_id(ws, eid)
    except Exception as e:
        print("employee_delete google sheets error:", e)
        flash("تم حذف الموظف، لكن فشل حذف السطر من Google Sheets", "warning")
        return redirect(url_for("employees_list"))

    flash("تم حذف الموظف", "info")
    return redirect(url_for("employees_list"))

# =========================
# Tasks
# =========================
@app.route("/tasks", methods=["GET", "POST"])
@login_required
@permission_required("tasks")
def tasks():
    db = get_db()

    # تأكد من الجداول المطلوبة
    try:
        ensure_financial_commitments_schema(db)
    except Exception as e:
        print("ensure_financial_commitments_schema error:", e)

    try:
        ensure_employees_schema(db)
    except Exception as e:
        print("ensure_employees_schema error:", e)

    # إضافة مهمة يدوية
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        due_date = (request.form.get("due_date") or "").strip() or None

        if title:
            created_at = datetime.now().isoformat()
            db.execute(
                "INSERT INTO tasks (title, due_date, created_at) VALUES (?, ?, ?)",
                (title, due_date, created_at),
            )
            db.commit()
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Google Sheets (tasks_manual)
            try:
                ws, err = open_ws("tasks_manual")
                if not err:
                    headers = ["id", "title", "due_date", "created_at"]
                    ok = ws_upsert(ws, headers, [new_id, title, due_date or "", created_at], new_id)
                    if not ok:
                        flash("⚠️ فشل تصدير Google Sheets (tasks_manual/add) راجع logs", "warning")
            except Exception as e:
                print("Google Sheets export (tasks_manual) error:", e)

        return redirect(url_for("tasks"))

    # (A) الالتزامات المالية
    try:
        commitments = db.execute("""
            SELECT id, party, amount, task_id, created_at
              FROM financial_commitments
             ORDER BY id DESC
        """).fetchall()
    except Exception as e:
        print("commitments fetch error:", e)
        commitments = []

    # (B) المهام اليدوية فقط (نستبعد المرتبط بالتزامات)
    try:
        tasks_list = db.execute("""
            SELECT t.id, t.title, t.due_date, t.created_at
              FROM tasks t
             WHERE t.id NOT IN (
                   SELECT COALESCE(task_id, 0)
                     FROM financial_commitments
                    WHERE task_id IS NOT NULL
             )
             ORDER BY t.id DESC
        """).fetchall()
    except Exception as e:
        print("tasks_list error:", e)
        tasks_list = []

    # (C) التنبيهات (سجلات + موظفين) ضمن 45 يوم
    alerts = []
    DAYS_WINDOW = 45

    def _safe_fromiso(s):
        try:
            return date.fromisoformat((s or "").strip())
        except Exception:
            return None

    # سجلات ES1/ES2/ES3
    for kind in ("ES1", "ES2", "ES3"):
        table = f"records_{kind.lower()}"
        try:
            recs = db.execute(f"""
                SELECT record_type, record_number, expiry_date
                  FROM {table}
                 WHERE expiry_date IS NOT NULL AND expiry_date <> ''
            """).fetchall()
        except Exception as e:
            print(f"records table error ({table}):", e)
            recs = []

        for r in recs:
            d = _safe_fromiso(r["expiry_date"])
            if not d:
                continue
            dl = (d - date.today()).days
            if dl > DAYS_WINDOW:
                continue

            alerts.append({
                "source": kind,
                "name": r["record_type"] or "",
                "number": r["record_number"] or "",
                "expiry": d.isoformat(),
                "days_left": dl,
                "color": status_color(d),
            })

    # موظفين: جواز/إقامة/تأمين/عقد
    try:
        emp_rows = db.execute("""
            SELECT name_ar,
                   passport_expiry,
                   iqama_expiry,
                   insurance_expiry,
                   contract_no,
                   contract_expiry
              FROM employees
        """).fetchall()
    except Exception as e:
        print("employees alerts error:", e)
        emp_rows = []

    def add_emp_alert(title, expiry, number="-"):
        d = _safe_fromiso(expiry)
        if not d:
            return
        dl = (d - date.today()).days
        if dl > DAYS_WINDOW:
            return
        alerts.append({
            "source": "الموظفين",
            "name": title,
            "number": number or "-",
            "expiry": d.isoformat(),
            "days_left": dl,
            "color": status_color(d),
        })

    for e in emp_rows:
        name = (e["name_ar"] or "").strip()
        add_emp_alert(f"انتهاء جواز {name}", e["passport_expiry"])
        add_emp_alert(f"انتهاء إقامة {name}", e["iqama_expiry"])
        add_emp_alert(f"انتهاء تأمين {name}", e["insurance_expiry"])
        add_emp_alert(
            f"انتهاء عقد {name}",
            e["contract_expiry"],
            e["contract_no"] or "-"
        )

    alerts.sort(key=lambda x: x["expiry"] or "9999-12-31")
    # --- تصدير tasks_auto (Snapshot) ---
       # --- تصدير tasks_auto (Snapshot) ---
    try:
        headers = ["source", "name", "number", "expiry", "days_left", "color"]

        rows_out = [
            [
                a.get("source", ""),
                a.get("name", ""),
                a.get("number", ""),
                a.get("expiry", ""),
                a.get("days_left", ""),
                a.get("color", ""),
            ]
            for a in alerts
        ]

        ok = ws_replace_all("tasks_auto", headers, rows_out)
        if not ok:
            print("tasks_auto export failed")

    except Exception as e:
        print("tasks_auto export exception:", e)


    return render_template(
        "tasks.html",
        tasks=tasks_list,
        rows=alerts,
        commitments=commitments
    )


@app.route("/tasks/<int:tid>/delete", methods=["POST"])
@login_required
@permission_required("tasks")
def task_delete(tid: int):
    db = get_db()

    try:
        linked = db.execute(
            "SELECT 1 FROM financial_commitments WHERE task_id=? LIMIT 1",
            (tid,),
        ).fetchone()
        if linked:
            flash("هذه المهمة مرتبطة بالتزام مالي ولا يمكن حذفها من هنا.", "warning")
            return redirect(url_for("tasks"))
    except Exception as e:
        print("task_delete linked-check error:", e)

    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()

    try:
        ws, err = open_ws("tasks_manual")
        if not err:
            ws_delete_by_id(ws, tid)
    except Exception as e:
        print("Google Sheets delete (tasks_manual) error:", e)

    flash("تم حذف المهمة اليدوية", "info")
    return redirect(url_for("tasks"))


# =========================
# Permissions (master only)
# =========================
@app.route("/permissions", methods=["GET", "POST"])
@login_required
@admin_required
def permissions_page():
    db = get_db()

    if request.method == "POST" and request.form.get("action") == "add_user":
        username = request.form.get("new_username", "").strip()
        password = request.form.get("new_password", "").strip()

        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة", "warning")
            return redirect(url_for("permissions_page"))

        try:
            db.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 0, ?)",
                (username, generate_password_hash(password), datetime.now().isoformat()),
            )
            db.commit()

            uid = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            for k in PERM_KEYS:
                db.execute(
                    "INSERT INTO permissions (user_id, perm_key, allowed) VALUES (?, ?, 0)",
                    (uid, k),
                )
            db.commit()

            flash("تم إضافة المستخدم", "success")
        except sqlite3.IntegrityError:
            flash("اسم المستخدم موجود مسبقاً", "danger")

        return redirect(url_for("permissions_page"))

    if request.method == "POST" and request.form.get("action") == "update_user":
        uid = int(request.form.get("uid", "0") or 0)

        new_pass = request.form.get("password", "").strip()
        if new_pass:
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new_pass), uid),
            )

        for k in PERM_KEYS:
            allowed = 1 if request.form.get(f"perm_{k}") == "on" else 0
            db.execute(
                """
                INSERT INTO permissions (user_id, perm_key, allowed)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, perm_key) DO UPDATE SET allowed=excluded.allowed
                """,
                (uid, k, allowed),
            )

        db.commit()

        if session.get("user_id") == uid:
            session["perms"] = load_user_perms(uid)

        flash("تم تحديث الصلاحيات", "success")
        return redirect(url_for("permissions_page"))

    if request.method == "POST" and request.form.get("action") == "delete_user":
        uid = int(request.form.get("uid", "0") or 0)

        master_row = db.execute("SELECT id FROM users WHERE username=?", (MASTER_USERNAME,)).fetchone()
        if master_row and uid == master_row["id"]:
            flash("لا يمكن حذف المستخدم الرئيسي", "danger")
            return redirect(url_for("permissions_page"))

        db.execute("DELETE FROM permissions WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()

        flash("تم حذف المستخدم", "info")
        return redirect(url_for("permissions_page"))

    users = db.execute("SELECT id, username FROM users ORDER BY id ASC").fetchall()
    user_perms = {}
    for u in users:
        pr = db.execute(
            "SELECT perm_key, allowed FROM permissions WHERE user_id=?",
            (u["id"],),
        ).fetchall()
        user_perms[u["id"]] = {r["perm_key"]: bool(r["allowed"]) for r in pr}

    return render_template(
        "permissions.html",
        users=users,
        user_perms=user_perms,
        perm_keys=PERM_KEYS,
        labels=PERM_LABELS,
        master=MASTER_USERNAME,
    )


# Debt collection followups and fixed WhatsApp sender.
from debt_collection_ext import install_debt_collection, debt_message_decision
install_debt_collection(app,get_db,ensure_debts_ledger_schema,
                        ensure_whatsapp_schema,_wa_allowed_number_ids,
                        _wa_normalize_destination,build_debt_whatsapp_message)

# Shared, non-destructive customer directory (all debts remain independent).
from debt_customers_ext import install_customer_directory, normalized_name, normalized_phone, customer_matches
DEBT_CUSTOMERS = install_customer_directory(app, get_db, ensure_debts_ledger_schema,
                                            has_debt_permission, MASTER_USERNAME)


# =========================
# Run (Local)
# =========================
if __name__ == "__main__":
    # أول مرة فقط إذا قاعدة البيانات جديدة:
    # init_db()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
