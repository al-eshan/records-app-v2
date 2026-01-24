# -*- coding: utf-8 -*-
import os
import json
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask, g, render_template,
    request, redirect, url_for, session, flash
)

from werkzeug.security import generate_password_hash, check_password_hash

# Google Sheets (Service Account)
import gspread
from google.oauth2.service_account import Credentials as SA_Credentials


# =========================
# App Config / Constants
# =========================
APP_TITLE = "نظام السجلات والموظفين"
BASE_DIR = os.path.dirname(__file__)
DATABASE = os.path.join(BASE_DIR, "data.db")

# المستخدم الرئيسي (فقط هذا يدخل صفحة الصلاحيات)
MASTER_USERNAME = "adm-es"

# Google Sheets Scope (Service Account)
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# صلاحيات النظام
PERM_KEYS = [
    "home",
    "accounting_home",
    "accounting_es1",
    "accounting_es2",
    "accounting_es3",
    "monthly_commission",
    "financial_commitments",
    "records_home",
    "records_es1",
    "records_es2",
    "records_es3",
    "employees",
    "tasks",
    "permissions",
]

PERM_LABELS = {
    "home": "الرئيسية",
    "accounting_home": "المحاسبة المالية ",
    "accounting_es1": "محاسبة يومية ES1",
    "accounting_es2": "محاسبة يومية ES2",
    "accounting_es3": "محاسبة يومية ES3",
    "monthly_commission": "عمولة المبيعات",
    "financial_commitments": "الالتزامات المالية",
    "records_home": "السجلات",
    "records_es1": "سجلات ES1",
    "records_es2": "سجلات ES2",
    "records_es3": "سجلات ES3",
    "employees": "الموظفين",
    "tasks": "المهام",
    "permissions": "الصلاحيات",
}

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-this-secret")


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

    db = get_db()

    # إذا ما فيه جدول users غالبًا قاعدة جديدة
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

    # أسماء الأعمدة من PRAGMA table_info
    cols = [r[1] for r in db.execute("PRAGMA table_info(financial_commitments)").fetchall()]
    if "task_id" not in cols:
        db.execute("ALTER TABLE financial_commitments ADD COLUMN task_id INTEGER")

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

    # تأكد من وجود الصلاحيات كلها للمستخدم الرئيسي
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
    # Bootstrap DB schema + master user (خفيف بعد أول مرة)
    try:
        ensure_db_schema_once()
        db = get_db()
        ensure_financial_commitments_schema(db)
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

    # تحديث مهمة موجودة
    if row["task_id"]:
        db.execute("UPDATE tasks SET title=? WHERE id=?", (title, row["task_id"]))
        db.commit()
        return

    # إنشاء مهمة جديدة
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
# Google Sheets (Service Account ONLY)
# =========================
def get_gs_client():
    """
    يعتمد على Service Account JSON داخل متغير البيئة:
    GOOGLE_SERVICE_ACCOUNT_JSON = محتوى JSON كامل
    GOOGLE_SHEETS_ID = Spreadsheet ID
    """
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        return None, "missing_service_account_json"

    try:
        creds = SA_Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=GOOGLE_SCOPES,
        )
        client = gspread.authorize(creds)
        return client, None
    except Exception:
        return None, "invalid_service_account_json"


def open_ws(tab_name: str):
    """Open worksheet by tab name. Returns (ws, err)."""
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not sheet_id:
        return None, "missing_sheet_id"

    client, err = get_gs_client()
    if err:
        return None, err

    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        return None, "worksheet_not_found"
    return ws, None


def ws_ensure_headers(ws, headers):
    values = ws.get_all_values()
    if not values:
        ws.append_row(headers)
        return
    if values and values[0] != headers:
        ws.delete_rows(1)
        ws.insert_row(headers, 1)


def ws_upsert(ws, headers, row_values, row_id):
    """Upsert by id in column A."""
    ws_ensure_headers(ws, headers)
    all_vals = ws.get_all_values()

    target_row = None
    for i, r in enumerate(all_vals[1:], start=2):
        if r and r[0] == str(row_id):
            target_row = i
            break

    end_col = chr(64 + len(headers))  # A,B,C...
    if target_row:
        ws.update(f"A{target_row}:{end_col}{target_row}", [row_values])
    else:
        ws.append_row(row_values)


def ws_delete_by_id(ws, row_id):
    all_vals = ws.get_all_values()
    for i, r in enumerate(all_vals[1:], start=2):
        if r and r[0] == str(row_id):
            ws.delete_rows(i)
            return True
    return False


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


@app.context_processor
def inject_helpers():
    return {
        "APP_TITLE": APP_TITLE,
        "days_left": days_left,
        "status_color": status_color,
        "perm_labels": PERM_LABELS,
        "perm_keys": PERM_KEYS,
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


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/home")
@login_required
@permission_required("home")
def home():
    return render_template("home.html")


# =========================
# Accounting (UI)
# =========================
@app.route("/accounting")
@login_required
@permission_required("accounting_home")
def accounting_home():
    return render_template("accounting_home.html")


def ensure_daily_accounting_schema(db):
    """Create daily accounting tables if missing (safe on existing DB)."""
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_header (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          es TEXT NOT NULL,
          day_date TEXT NOT NULL,
          cash_start REAL NOT NULL DEFAULT 0,
          cash_end   REAL NOT NULL DEFAULT 0,
          notes TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT,
          UNIQUE(es, day_date)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_inputs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          input_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_expenses_general (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          expense_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_expenses_petty (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          header_id INTEGER NOT NULL,
          expense_type TEXT NOT NULL,
          amount REAL NOT NULL DEFAULT 0,
          FOREIGN KEY(header_id) REFERENCES daily_header(id) ON DELETE CASCADE
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_daily_header_es_date ON daily_header(es, day_date)")

    cols = [r[1] for r in db.execute("PRAGMA table_info(daily_header)").fetchall()]
    if "total_in_enjaz" not in cols:
        db.execute("ALTER TABLE daily_header ADD COLUMN total_in_enjaz REAL NOT NULL DEFAULT 0")

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
        return float(s)
    except Exception:
        return float(default)


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

    if request.method == "POST":
        cash_start = _to_float(request.form.get("cash_start"), 0)
        cash_end = _to_float(request.form.get("cash_end"), 0)
        total_in_enjaz = _to_float(request.form.get("total_in_enjaz"), 0)
        notes = (request.form.get("notes") or "").strip() or None

        db.execute(
            "INSERT OR IGNORE INTO daily_header (es, day_date, created_at) VALUES (?, ?, datetime('now'))",
            (kind, day_date),
        )
        header = db.execute(
            "SELECT id FROM daily_header WHERE es=? AND day_date=?",
            (kind, day_date),
        ).fetchone()
        header_id = header["id"]

        db.execute(
            "UPDATE daily_header SET cash_start=?, cash_end=?, total_in_enjaz=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (cash_start, cash_end, total_in_enjaz, notes, header_id),
        )

        db.execute("DELETE FROM daily_inputs WHERE header_id=?", (header_id,))
        db.execute("DELETE FROM daily_expenses_general WHERE header_id=?", (header_id,))
        db.execute("DELETE FROM daily_expenses_petty WHERE header_id=?", (header_id,))

        input_types = request.form.getlist("input_type")
        input_amounts = request.form.getlist("input_amount")
        for t, a in zip(input_types, input_amounts):
            t = (t or "").strip()
            amt = _to_float(a, 0)
            if t or amt:
                if not t:
                    t = "(بدون عنوان)"
                db.execute(
                    "INSERT INTO daily_inputs (header_id, input_type, amount) VALUES (?, ?, ?)",
                    (header_id, t, amt),
                )

        gen_types = request.form.getlist("gen_type")
        gen_amounts = request.form.getlist("gen_amount")
        for t, a in zip(gen_types, gen_amounts):
            t = (t or "").strip()
            amt = _to_float(a, 0)
            if t or amt:
                if not t:
                    t = "(بدون عنوان)"
                db.execute(
                    "INSERT INTO daily_expenses_general (header_id, expense_type, amount) VALUES (?, ?, ?)",
                    (header_id, t, amt),
                )

        petty_types = request.form.getlist("petty_type")
        petty_amounts = request.form.getlist("petty_amount")
        for t, a in zip(petty_types, petty_amounts):
            t = (t or "").strip()
            amt = _to_float(a, 0)
            if t or amt:
                if not t:
                    t = "(بدون عنوان)"
                db.execute(
                    "INSERT INTO daily_expenses_petty (header_id, expense_type, amount) VALUES (?, ?, ?)",
                    (header_id, t, amt),
                )

        db.commit()
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
            "SELECT expense_type, amount FROM daily_expenses_general WHERE header_id=? ORDER BY id",
            (header_id,),
        ).fetchall()
        petty = db.execute(
            "SELECT expense_type, amount FROM daily_expenses_petty WHERE header_id=? ORDER BY id",
            (header_id,),
        ).fetchall()

    def _sum(rows):
        return float(sum([_to_float(r["amount"], 0) for r in rows]))

    totals = {
        "inputs": _sum(inputs),
        "general": _sum(gen),
        "petty": _sum(petty),
    }
    totals["expenses"] = totals["general"] + totals["petty"]

    cash_start = _to_float(header["cash_start"], 0) if header else 0.0
    cash_end = _to_float(header["cash_end"], 0) if header else 0.0
    total_in_enjaz = _to_float(header["total_in_enjaz"], 0) if header else 0.0

    totals["cash_start"] = cash_start
    totals["cash_end"] = cash_end
    totals["total_overall"] = totals["inputs"] + totals["expenses"] + cash_end - cash_start
    totals["total_in_enjaz"] = total_in_enjaz
    totals["error"] = totals["total_overall"] - totals["total_in_enjaz"]

    return render_template(
        "accounting_daily.html",
        kind=kind,
        day_date=day_date,
        header=header,
        inputs=inputs,
        gen=gen,
        petty=petty,
        totals=totals,
    )


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

    headers = db.execute("""
        SELECT id, day_date, cash_start, cash_end, total_in_enjaz
          FROM daily_header
         WHERE es=?
           AND day_date BETWEEN ? AND ?
         ORDER BY day_date DESC
    """, (kind, date_from, date_to)).fetchall()

    rows = []
    overall_inputs = overall_general = overall_petty = 0.0
    overall_cash_start = overall_cash_end = overall_total_in_enjaz = 0.0

    for h in headers:
        hid = h["id"]

        total_inputs = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_inputs WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        total_general = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_expenses_general WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        total_petty = _to_float(db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM daily_expenses_petty WHERE header_id=?",
            (hid,)
        ).fetchone()["s"], 0)

        expenses = total_general + total_petty
        cash_start = _to_float(h["cash_start"], 0)
        cash_end = _to_float(h["cash_end"], 0)
        total_in_enjaz = _to_float(h["total_in_enjaz"], 0)

        total_overall = total_inputs + expenses + cash_end - cash_start
        err_val = total_overall - total_in_enjaz

        rows.append({
            "day_date": h["day_date"],
            "cash_start": cash_start,
            "cash_end": cash_end,
            "total_inputs": total_inputs,
            "total_general": total_general,
            "total_petty": total_petty,
            "expenses": expenses,
            "total_in_enjaz": total_in_enjaz,
            "total_overall": total_overall,
            "error": err_val,
        })

        overall_inputs += total_inputs
        overall_general += total_general
        overall_petty += total_petty
        overall_cash_start += cash_start
        overall_cash_end += cash_end
        overall_total_in_enjaz += total_in_enjaz

    overall_expenses = overall_general + overall_petty
    overall_total_overall = overall_inputs + overall_expenses + overall_cash_end - overall_cash_start
    overall_error = overall_total_overall - overall_total_in_enjaz

    overall = {
        "inputs": overall_inputs,
        "general": overall_general,
        "petty": overall_petty,
        "expenses": overall_expenses,
        "cash_start": overall_cash_start,
        "cash_end": overall_cash_end,
        "total_in_enjaz": overall_total_in_enjaz,
        "total_overall": overall_total_overall,
        "error": overall_error,
    }

    return render_template(
        "accounting_movements.html",
        kind=kind,
        date_from=date_from,
        date_to=date_to,
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
                create_or_update_commitment_task(db, cur.lastrowid)

        elif action == "update":
            cid = int(request.form.get("id"))
            party = (request.form.get("party") or "").strip()
            amount = _to_float(request.form.get("amount"), 0)

            db.execute(
                "UPDATE financial_commitments SET party=?, amount=?, updated_at=datetime('now') WHERE id=?",
                (party, amount, cid),
            )
            db.commit()
            create_or_update_commitment_task(db, cid)

        elif action == "delete":
            cid = int(request.form.get("id"))
            delete_commitment_task(db, cid)
            db.execute("DELETE FROM financial_commitments WHERE id=?", (cid,))
            db.commit()

        return redirect(url_for("accounting_commitments_home"))

    rows = db.execute("SELECT * FROM financial_commitments ORDER BY id DESC").fetchall()
    total = float(sum([_to_float(r["amount"], 0) for r in rows]))

    return render_template("accounting_commitments_home.html", rows=rows, total=total)


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
            flash("تم تعديل العمولة", "success")
        else:
            db.execute("""
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
            flash("تم حفظ العمولة", "success")

        db.commit()
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

    if request.method == "POST":
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

        ws, err = open_ws(f"records_{kind.lower()}")
        if not err:
            headers = ["id", "record_type", "record_number", "expiry_date"]
            ws_upsert(ws, headers, [new_id, record_type, record_number, expiry_date or ""], new_id)

        flash("تمت الإضافة بنجاح", "success")
        return redirect(url_for("records_list", kind=kind))

    return render_template("record_form.html", kind=kind, mode="new", row=None)


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
            ws_upsert(ws, headers, [rid, record_type, record_number, expiry_date or ""], rid)

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
@app.route("/employees")
@login_required
@permission_required("employees")
def employees_list():
    db = get_db()
    rows = db.execute("""
        SELECT id, name_ar, name_en, nationality, mobile,
               passport_no, passport_expiry,
               iqama_no, iqama_expiry,
               insurance_name, insurance_expiry,
               basic_salary, commission, bank_account
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
            "passport_no": r["passport_no"],
            "passport_expiry": parse_date(r["passport_expiry"]) if r["passport_expiry"] else None,
            "iqama_no": r["iqama_no"],
            "iqama_expiry": parse_date(r["iqama_expiry"]) if r["iqama_expiry"] else None,
            "insurance_name": r["insurance_name"],
            "insurance_expiry": parse_date(r["insurance_expiry"]) if r["insurance_expiry"] else None,
            "basic_salary": r["basic_salary"],
            "commission": r["commission"],
            "bank_account": r["bank_account"],
        })
    return render_template("employees_list.html", rows=data)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
@permission_required("employees")
def employee_new():
    if request.method == "POST":
        db = get_db()
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
            "basic_salary": request.form.get("basic_salary", "").strip() or None,
            "commission": request.form.get("commission", "").strip() or None,
            "bank_account": request.form.get("bank_account", "").strip(),
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
              basic_salary, commission, bank_account
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload["name_ar"], payload["name_en"], payload["nationality"], payload["mobile"],
            payload["passport_no"], payload["passport_expiry"],
            payload["iqama_no"], payload["iqama_expiry"],
            payload["insurance_name"], payload["insurance_expiry"],
            payload["basic_salary"], payload["commission"], payload["bank_account"],
        ))
        db.commit()
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        ws, err = open_ws("employees")
        if not err:
            headers = [
                "id","name_ar","name_en","nationality","mobile",
                "passport_no","passport_expiry",
                "iqama_no","iqama_expiry",
                "insurance_name","insurance_expiry",
                "basic_salary","commission","bank_account"
            ]
            ws_upsert(ws, headers, [
                new_id,
                payload["name_ar"], payload["name_en"],
                payload["nationality"], payload["mobile"],
                payload["passport_no"], payload["passport_expiry"] or "",
                payload["iqama_no"], payload["iqama_expiry"] or "",
                payload["insurance_name"], payload["insurance_expiry"] or "",
                payload["basic_salary"] or "", payload["commission"] or "",
                payload["bank_account"],
            ], new_id)

        flash("تمت إضافة الموظف", "success")
        return redirect(url_for("employees_list"))

    return render_template("employee_form.html", mode="new", row=None)


@app.route("/employees/<int:eid>")
@login_required
@permission_required("employees")
def employee_detail(eid: int):
    db = get_db()
    r = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if r is None:
        flash("الموظف غير موجود", "danger")
        return redirect(url_for("employees_list"))

    row = dict(r)
    for k in ("passport_expiry", "iqama_expiry", "insurance_expiry"):
        row[k] = parse_date(row[k]) if row.get(k) else None

    return render_template("employee_detail.html", row=row)


@app.route("/employees/<int:eid>/edit", methods=["GET", "POST"])
@login_required
@permission_required("employees")
def employee_edit(eid: int):
    db = get_db()
    row = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if row is None:
        flash("الموظف غير موجود", "danger")
        return redirect(url_for("employees_list"))

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
            "basic_salary": request.form.get("basic_salary", "").strip() or None,
            "commission": request.form.get("commission", "").strip() or None,
            "bank_account": request.form.get("bank_account", "").strip(),
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
                basic_salary=?,
                commission=?,
                bank_account=?
            WHERE id=?
        """, (
            payload["name_ar"], payload["name_en"],
            payload["nationality"], payload["mobile"],
            payload["passport_no"], payload["passport_expiry"],
            payload["iqama_no"], payload["iqama_expiry"],
            payload["insurance_name"], payload["insurance_expiry"],
            payload["basic_salary"], payload["commission"],
            payload["bank_account"], eid,
        ))
        db.commit()

        ws, err = open_ws("employees")
        if not err:
            headers = [
                "id","name_ar","name_en","nationality","mobile",
                "passport_no","passport_expiry",
                "iqama_no","iqama_expiry",
                "insurance_name","insurance_expiry",
                "basic_salary","commission","bank_account"
            ]
            ws_upsert(ws, headers, [
                eid,
                payload["name_ar"], payload["name_en"],
                payload["nationality"], payload["mobile"],
                payload["passport_no"], payload["passport_expiry"] or "",
                payload["iqama_no"], payload["iqama_expiry"] or "",
                payload["insurance_name"], payload["insurance_expiry"] or "",
                payload["basic_salary"] or "", payload["commission"] or "",
                payload["bank_account"],
            ], eid)

        flash("تم تعديل بيانات الموظف", "success")
        return redirect(url_for("employee_detail", eid=eid))

    return render_template("employee_form.html", mode="edit", row=row)

@app.route("/employees/<int:eid>/delete", methods=["POST"])
@login_required
@permission_required("employees")
def employee_delete(eid: int):
    db = get_db()

    # تأكد أن الموظف موجود
    row = db.execute("SELECT id FROM employees WHERE id=?", (eid,)).fetchone()
    if not row:
        flash("الموظف غير موجود", "warning")
        return redirect(url_for("employees_list"))

    try:
        db.execute("DELETE FROM employees WHERE id=?", (eid,))
        db.commit()
    except Exception as e:
        db.rollback()
        # اطبع الخطأ في اللوق (مفيد في Render)
        print("employee_delete error:", e)
        flash("تعذر حذف الموظف (راجع السجلات).", "danger")
        return redirect(url_for("employees_list"))

    # حذف من Google Sheets (اختياري)
    ws, err = open_ws("employees")
    if not err:
        ws_delete_by_id(ws, eid)

    flash("تم حذف الموظف", "info")
    return redirect(url_for("employees_list"))

@app.route("/tasks", methods=["GET", "POST"])
@login_required
@permission_required("tasks")
def tasks():
    db = get_db()

    # تأكد من جدول الالتزامات (مع task_id)
    try:
        ensure_commitments_schema(db)
    except Exception as e:
        print("ensure_commitments_schema error:", e)

    # =========
    # إضافة مهمة يدوية
    # =========
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

            # تصدير Google Sheets اختياري (لا يكسر الصفحة لو ناقص الإعدادات)
            try:
                ws, err = open_ws("tasks_manual")
                if not err:
                    headers = ["id", "title", "due_date", "created_at"]
                    ws_upsert(ws, headers, [new_id, title, due_date or "", created_at], new_id)
            except Exception as e:
                print("Google Sheets export (tasks_manual) error:", e)

        return redirect(url_for("tasks"))

    # =========
    # 1) المهام اليدوية
    # =========
    tasks_list = []
    try:
        tasks_list = db.execute(
            "SELECT id, title, due_date, created_at FROM tasks ORDER BY id DESC"
        ).fetchall()
    except Exception as e:
        print("tasks_list error:", e)
        tasks_list = []

    # =========
    # 2) التنبيهات (من السجلات + الموظفين) ضمن 45 يوم
    # =========
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
            recs = db.execute(
                f"""
                SELECT record_type, record_number, expiry_date
                FROM {table}
                WHERE expiry_date IS NOT NULL AND expiry_date <> ''
                """
            ).fetchall()
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

    # موظفين: جواز/إقامة/تأمين
    try:
        emp_rows = db.execute("""
            SELECT name_ar, passport_expiry, iqama_expiry, insurance_expiry
            FROM employees
        """).fetchall()
    except Exception as e:
        print("employees alerts error:", e)
        emp_rows = []

    def add_emp_alert(title, expiry):
        d = _safe_fromiso(expiry)
        if not d:
            return
        dl = (d - date.today()).days
        if dl > DAYS_WINDOW:
            return
        alerts.append({
            "source": "الموظفين",
            "name": title,
            "number": "-",
            "expiry": d.isoformat(),
            "days_left": dl,
            "color": status_color(d),
        })

    for e in emp_rows:
        name = (e["name_ar"] or "").strip()
        add_emp_alert(f"انتهاء جواز {name}", e["passport_expiry"])
        add_emp_alert(f"انتهاء إقامة {name}", e["iqama_expiry"])
        add_emp_alert(f"انتهاء تأمين {name}", e["insurance_expiry"])

    alerts.sort(key=lambda x: x["expiry"] or "9999-12-31")

    # =========
    # 3) الالتزامات المالية (للعرض في صفحة المهام)
    # =========
    try:
        commitments = db.execute("""
            SELECT id, party, amount, task_id, created_at
            FROM financial_commitments
            ORDER BY id DESC
        """).fetchall()
    except Exception as e:
        print("commitments fetch error:", e)
        commitments = []

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
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()

    ws, err = open_ws("tasks_manual")
    if not err:
        ws_delete_by_id(ws, tid)

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


# =========================
# Run (Local)
# =========================
if __name__ == "__main__":
    # أول مرة فقط إذا قاعدة البيانات جديدة:
    # شغّل init_db() مرة واحدة محليًا ثم رجّعها تعليق:
    # init_db()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
