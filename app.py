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
DATABASE = os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "data.db")

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
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
# Render وكل منصات الاستضافة غالباً HTTPS
if os.environ.get("COOKIE_SECURE", "1") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True



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




# =========================
# DB bootstrap (مفيد للإنتاج + Render Disk)
# =========================
_BOOTSTRAPPED = False

def bootstrap_db():
    """Create DB from schema.sql if DATABASE file doesn't exist yet."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    try:
        db_dir = os.path.dirname(DATABASE)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        if not os.path.exists(DATABASE):
            init_db()
    except Exception:
        # لا نوقف التشغيل إذا فشل الإنشاء (قد تكون قاعدة البيانات مختلفة لاحقًا مثل PostgreSQL)
        pass

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
        uid = db.execute("SELECT id FROM users WHERE username=?", (MASTER_USERNAME,)).fetchone()["id"]

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
    # شغّل Bootstrap مرة واحدة فقط: إنشاء DB إذا غير موجودة + إنشاء المستخدم الرئيسي.
    bootstrap_db()
    try:
        ensure_master_user()
    except Exception:
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
    row = db.execute("SELECT task_id FROM financial_commitments WHERE id=?", (commitment_id,)).fetchone()
    if row and row["task_id"]:
        db.execute("DELETE FROM tasks WHERE id=?", (row["task_id"],))
        db.commit()




def ensure_commitments_schema(db):
    """Create/upgrade financial_commitments table (includes task_id)."""
    ensure_commitments_schema(db)

    commitments = db.execute("""
        SELECT id, party, amount, created_at
        FROM financial_commitments
        ORDER BY id DESC
    """).fetchall()

    return render_template("tasks.html", tasks=tasks_list, rows=rows, commitments=commitments)


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
