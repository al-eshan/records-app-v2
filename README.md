# Records App (v2)

Flask admin system (Al‑Eshan).

- Deploy: **Render**
- Repo: **GitHub**
- DB: **SQLite الآن** (مع Render Persistent Disk) → **PostgreSQL لاحقًا** (خطوة قادمة)
- Google Sheets: Service Account (تصدير/نسخ احتياطي فقط)

---

## المتطلبات
- Python 3.11+
- Git

---

## تشغيل محلي (Windows PowerShell)

> أول مرة فقط: أنشئ قاعدة البيانات بتشغيل `schema.sql` تلقائيًا (صار يعمل تلقائيًا إذا الملف غير موجود).

```powershell
$env:APP_SECRET="change-me"
$env:MASTER_PASS="1234"   # كلمة مرور المستخدم الرئيسي adm-es (اختياري)
# اختيارية (للتصدير إلى Google Sheets)
$env:GOOGLE_SHEETS_ID="..."
$env:GOOGLE_SERVICE_ACCOUNT_JSON='{}'

python app.py
```

افتح:
- http://127.0.0.1:5000

---

## النشر على Render (GitHub + Render)

### 1) رفع المشروع على GitHub
من داخل مجلد المشروع:

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<USERNAME>/<REPO>.git
git push -u origin main
```

### 2) إنشاء Web Service في Render
- New → **Web Service**
- اختر الريبو من GitHub

**Build Command**
```
pip install -r requirements.txt
```

**Start Command**
```
gunicorn app:app --bind 0.0.0.0:$PORT
```

### 3) Environment Variables (مهم)
في Render → Settings → Environment:

- `APP_SECRET` = قيمة قوية (لا تتركها الافتراضية)
- `MASTER_PASS` = كلمة مرور المستخدم الرئيسي `adm-es` (اختياري)
- (اختياري للـ Sheets)
  - `GOOGLE_SHEETS_ID`
  - `GOOGLE_SERVICE_ACCOUNT_JSON`  (ضع JSON كامل كسطر واحد)

### 4) SQLite على Render (لازم Persistent Disk)
لأن SQLite يحتاج تخزين دائم:

- Render → Settings → **Disks** → Add Disk
  - Mount Path: `/var/data`
- أضف متغير بيئة:
  - `DATABASE_PATH=/var/data/data.db`

بدون Disk: قاعدة البيانات ستختفي عند إعادة التشغيل/الديبلوي.

---

## ملاحظات أمان سريعة
- غيّر `APP_SECRET` لقيمة قوية.
- لا ترفع ملف `data.db` إلى GitHub (موجود في `.gitignore`).
- لا تضع Service Account JSON داخل الكود؛ فقط في Environment Variables.

