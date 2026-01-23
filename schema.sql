PRAGMA foreign_keys = ON;

-- =========================
-- users
-- =========================
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

-- =========================
-- permissions: one row per (user, perm_key)
-- =========================
CREATE TABLE IF NOT EXISTS permissions (
  user_id INTEGER NOT NULL,
  perm_key TEXT NOT NULL,
  allowed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, perm_key),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- =========================
-- Monthly Commissions (الشهور)
-- =========================
CREATE TABLE IF NOT EXISTS monthly_commissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month TEXT NOT NULL UNIQUE,              -- YYYY-MM
  sales_before_tax REAL NOT NULL DEFAULT 0,
  total_commission REAL NOT NULL DEFAULT 0,
  employee_commission REAL NOT NULL DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);

-- =========================
-- Monthly Commission Settings (إعدادات العمولة: صف واحد فقط id=1)
-- =========================
CREATE TABLE IF NOT EXISTS monthly_commission_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  commission_rate REAL NOT NULL DEFAULT 0.008,  -- 0.8% = 0.008
  fixed_deduction REAL NOT NULL DEFAULT 200,    -- 200
  employees_count INTEGER NOT NULL DEFAULT 6,   -- 6
  updated_at TEXT
);

-- row default (id=1)
INSERT OR IGNORE INTO monthly_commission_settings
(id, commission_rate, fixed_deduction, employees_count, updated_at)
VALUES
(1, 0.008, 200, 6, datetime('now'));

-- =========================
-- records tables
-- =========================
CREATE TABLE IF NOT EXISTS records_es1 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_type TEXT,
  record_number TEXT NOT NULL,
  expiry_date TEXT
);

CREATE TABLE IF NOT EXISTS records_es2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_type TEXT,
  record_number TEXT NOT NULL,
  expiry_date TEXT
);

CREATE TABLE IF NOT EXISTS records_es3 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_type TEXT,
  record_number TEXT NOT NULL,
  expiry_date TEXT
);

-- =========================
-- employees
-- =========================
CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name_ar TEXT NOT NULL,
  name_en TEXT,
  nationality TEXT,
  mobile TEXT,

  passport_no TEXT,
  passport_expiry TEXT,

  iqama_no TEXT,
  iqama_expiry TEXT,

  insurance_name TEXT,
  insurance_expiry TEXT,

  basic_salary TEXT,
  commission TEXT,
  bank_account TEXT
);

-- =========================
-- manual tasks
-- =========================
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  due_date TEXT,
  created_at TEXT
);

-- =========================
-- Daily Accounting (المحاسبة اليومية) — معزولة لكل فرع ES
-- =========================
CREATE TABLE IF NOT EXISTS daily_header (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  es TEXT NOT NULL,                 -- ES1 / ES2 / ES3
  day_date TEXT NOT NULL,           -- YYYY-MM-DD
  cash_start REAL NOT NULL DEFAULT 0,
  cash_end   REAL NOT NULL DEFAULT 0,
  total_in_enjaz REAL NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT,
  UNIQUE(es, day_date)
);

CREATE TABLE IF NOT EXISTS daily_inputs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_id INTEGER NOT NULL,
  input_type TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  FOREIGN KEY (header_id) REFERENCES daily_header(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_expenses_general (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_id INTEGER NOT NULL,
  expense_type TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  FOREIGN KEY (header_id) REFERENCES daily_header(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_expenses_petty (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_id INTEGER NOT NULL,
  expense_type TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  FOREIGN KEY (header_id) REFERENCES daily_header(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_daily_header_es_date
ON daily_header(es, day_date);
-- جدول الالتزامات المالية (عام – بدون فروع)
CREATE TABLE IF NOT EXISTS financial_commitments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  party TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  task_id INTEGER,              -- ربط مباشر بالمهمة
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);
