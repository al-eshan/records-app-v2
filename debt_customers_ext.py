# -*- coding: utf-8 -*-
"""Safe customer directory for the debts ledger.

Debt/invoice records are never deduplicated or removed automatically.  A merge
only relinks their customer_id and archives the duplicate *profile*.
"""
import json
import re
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timezone
from flask import request, session, redirect, url_for, render_template, flash, jsonify


def normalized_name(value):
    s = unicodedata.normalize('NFKC', str(value or '')).strip().lower()
    s = re.sub('[\u064b-\u065f\u0670\u0640]', '', s)
    s = s.translate(str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ى': 'ي', 'ة': 'ه'}))
    s = re.sub(r'[^\w\u0600-\u06ff]+', ' ', s, flags=re.UNICODE)
    return ' '.join(s.split())


def normalized_phone(value):
    s = str(value or '').translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    s = ''.join(ch for ch in s if '0' <= ch <= '9')
    if s.startswith('00'):
        s = s[2:]
    if len(s) == 10 and s.startswith('05'):
        return '966' + s[1:]
    if len(s) == 9 and s.startswith('5'):
        return '966' + s
    if len(s) == 12 and s.startswith('9665'):
        return s
    # International numbers, without guessing their country code.
    return s if 10 <= len(s) <= 15 else ''


def ensure_customer_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS debt_customers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      normalized_name TEXT NOT NULL,
      mobile TEXT,
      normalized_phone TEXT,
      address TEXT,
      merged_into_customer_id INTEGER,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS debt_customer_policy (
      customer_id INTEGER PRIMARY KEY,
      policy TEXT NOT NULL DEFAULT 'pending',
      consent_at TEXT, consent_source TEXT, pause_until TEXT,
      dispute INTEGER NOT NULL DEFAULT 0, note TEXT, updated_by TEXT, updated_at TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS debt_customer_merge_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      target_customer_id INTEGER NOT NULL,
      source_customer_id INTEGER NOT NULL,
      linked_debts INTEGER NOT NULL,
      total_before REAL NOT NULL,
      total_after REAL NOT NULL,
      performed_by TEXT NOT NULL,
      performed_at TEXT DEFAULT (datetime('now')),
      note TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS debt_customer_override_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      new_customer_id INTEGER NOT NULL,
      reason TEXT NOT NULL,
      performed_by TEXT NOT NULL,
      performed_at TEXT DEFAULT (datetime('now'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS debt_duplicate_invoice_override_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      debt_id INTEGER NOT NULL, similar_debt_id INTEGER NOT NULL,
      reason TEXT NOT NULL, performed_by TEXT NOT NULL,
      performed_at TEXT DEFAULT (datetime('now'))
    )''')
    cols = {r[1] for r in db.execute('PRAGMA table_info(debts_ledger)').fetchall()}
    if 'customer_id' not in cols:
        db.execute('ALTER TABLE debts_ledger ADD COLUMN customer_id INTEGER')
    db.execute('CREATE INDEX IF NOT EXISTS idx_debt_customers_phone ON debt_customers(normalized_phone)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_debt_customers_name ON debt_customers(normalized_name)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_debts_customer_id ON debts_ledger(customer_id)')
    # Initial migration is intentionally one customer profile per historical row.
    # We do not make potentially incorrect identity merges based on phone/name.
    unlinked = db.execute('''SELECT id,customer_name,mobile,address FROM debts_ledger
      WHERE customer_id IS NULL ORDER BY id''').fetchall()
    for r in unlinked:
        cur = db.execute('''INSERT INTO debt_customers
          (name,normalized_name,mobile,normalized_phone,address)
          VALUES (?,?,?,?,?)''', (r['customer_name'] or '', normalized_name(r['customer_name']),
                                 r['mobile'] or '', normalized_phone(r['mobile']), r['address'] or ''))
        db.execute('UPDATE debts_ledger SET customer_id=? WHERE id=?', (cur.lastrowid, r['id']))
    db.commit()


def customer_matches(db, name, mobile, exclude_customer_id=None, limit=15):
    norm_name, norm_phone = normalized_name(name), normalized_phone(mobile)
    if not norm_name and not norm_phone:
        return []
    rows = db.execute('''SELECT * FROM debt_customers
        WHERE merged_into_customer_id IS NULL ORDER BY id''').fetchall()
    found = []
    for c in rows:
        if exclude_customer_id and c['id'] == exclude_customer_id:
            continue
        c_phone = c['normalized_phone'] or ''
        c_name = c['normalized_name'] or ''
        if norm_phone and c_phone and norm_phone == c_phone:
            kind = 'phone'
        elif len(norm_name) >= 3 and norm_name == c_name:
            kind = 'name'
        elif len(norm_name) >= 5 and c_name and min(len(norm_name),len(c_name)) >= 5 and (
                norm_name in c_name or c_name in norm_name or
                SequenceMatcher(None,norm_name,c_name).ratio() >= 0.86):
            kind = 'similar'
        else:
            continue
        found.append(({'phone':0,'name':1,'similar':2}[kind], c, kind))
    found.sort(key=lambda x: (x[0],x[1]['id']))
    return [{'customer':c,'kind':kind} for _,c,kind in found[:limit]]


def create_customer(db,name,mobile,address):
    cur=db.execute('''INSERT INTO debt_customers
      (name,normalized_name,mobile,normalized_phone,address)
      VALUES (?,?,?,?,?)''', (name,normalized_name(name),mobile,normalized_phone(mobile),address))
    return cur.lastrowid


def install_customer_directory(app,get_db,ensure_debts,has_debt_permission,master_username):
    def is_admin():
        return session.get('username') == master_username
    def authorized(es):
        return is_admin() or bool(has_debt_permission(es))
    def db_ready():
        db=get_db();ensure_debts(db);ensure_customer_schema(db);return db

    def display_matches(db,name,mobile,exclude=None):
        found=customer_matches(db,name,mobile,exclude)
        rendered=[]
        for obj in found:
            c=obj['customer']
            debts=db.execute('SELECT es,remaining_amount FROM debts_ledger WHERE customer_id=?',(c['id'],)).fetchall()
            visible=[r for r in debts if authorized(r['es'])]
            rendered.append({'id':c['id'],
                             'name':c['name'] if visible else 'عميل مسجل في فرع آخر',
                             'mobile':c['mobile'] if visible else '',
                             'kind':obj['kind'],'branches':sorted(set(r['es'] for r in visible)),
                             'other_branch':len(visible)<len(debts),
                             'amount':round(sum(float(r['remaining_amount'] or 0) for r in visible),2),
                             'debt_count':len(visible)})
        return rendered

    @app.route('/accounting/debts/customer-lookup',methods=['GET'])
    def debt_customer_lookup():
        if not session.get('logged_in'):
            return jsonify({'error':'not_logged_in'}),401
        if not any(authorized(es) for es in ('ES1','ES2','ES3')):
            return jsonify({'error':'forbidden'}),403
        name=(request.args.get('name') or '').strip()[:150]
        phone=(request.args.get('mobile') or '').strip()[:40]
        db=db_ready()
        matches=display_matches(db,name,phone)
        # Do not reveal another branch's financial details to staff.
        return jsonify({'matches':matches})

    @app.route('/accounting/debts/customer-duplicates',methods=['GET','POST'])
    def debt_customer_duplicates():
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not is_admin():
            return render_template('no_permission.html'),403
        db=db_ready()
        if request.method=='POST':
            try:
                target_id=int(request.form.get('target_customer_id') or 0)
                source_id=int(request.form.get('source_customer_id') or 0)
            except (TypeError,ValueError):
                target_id=source_id=0
            if target_id==source_id or not target_id or not source_id or request.form.get('confirm_merge')!='yes':
                flash('اختر سجلين مختلفين وأكد عملية الدمج.','danger')
                return redirect(url_for('debt_customer_duplicates'))
            a=db.execute('SELECT * FROM debt_customers WHERE id=? AND merged_into_customer_id IS NULL',(target_id,)).fetchone()
            b=db.execute('SELECT * FROM debt_customers WHERE id=? AND merged_into_customer_id IS NULL',(source_id,)).fetchone()
            if not a or not b:
                flash('أحد السجلين غير صالح أو تم دمجه مسبقًا.','danger')
                return redirect(url_for('debt_customer_duplicates'))
            candidates=customer_matches(db,a['name'],a['mobile'],limit=10000)
            if source_id not in [x['customer']['id'] for x in candidates]:
                flash('لا يوجد تطابق قوي بما يكفي؛ لا يمكن الدمج من هذه الصفحة.','danger')
                return redirect(url_for('debt_customer_duplicates'))
            # The balances belong to debt rows. Their sum must remain unchanged.
            before=db.execute('''SELECT COALESCE(SUM(remaining_amount),0) FROM debts_ledger
              WHERE customer_id IN (?,?)''',(target_id,source_id)).fetchone()[0] or 0
            source_count=db.execute('SELECT COUNT(*) FROM debts_ledger WHERE customer_id=?',(source_id,)).fetchone()[0]
            p_a=db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(target_id,)).fetchone()
            p_b=db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(source_id,)).fetchone()
            disputed=any(p and p['dispute'] for p in (p_a,p_b))
            with db:
                db.execute('UPDATE debts_ledger SET customer_id=? WHERE customer_id=?',(target_id,source_id))
                db.execute('UPDATE debt_customers SET merged_into_customer_id=?,updated_at=datetime(\'now\') WHERE id=?',(target_id,source_id))
                db.execute('''INSERT INTO debt_customer_policy(customer_id,policy,dispute,note,updated_by,updated_at)
                  VALUES (?,'pending',?,?,?,datetime('now'))
                  ON CONFLICT(customer_id) DO UPDATE SET policy='pending',consent_at=NULL,
                  consent_source=NULL,pause_until=NULL,dispute=excluded.dispute,
                  note=excluded.note,updated_by=excluded.updated_by,updated_at=datetime('now')''',
                  (target_id,int(disputed),'دمج ملفات العميل: يجب مراجعة موافقة التواصل مرة أخرى',session.get('username')))
                after=db.execute('''SELECT COALESCE(SUM(remaining_amount),0) FROM debts_ledger
                  WHERE customer_id=?''',(target_id,)).fetchone()[0] or 0
                if abs(float(after)-float(before)) > 0.005:
                    raise RuntimeError('Balance mismatch; merge rolled back')
                db.execute('''INSERT INTO debt_customer_merge_audit
                   (target_customer_id,source_customer_id,linked_debts,total_before,total_after,performed_by,note)
                   VALUES (?,?,?,?,?,?,?)''',
                   (target_id,source_id,source_count,before,after,session.get('username'),
                    (request.form.get('note') or '').strip()[:300]))
            flash('تم توحيد ملف العميل دون حذف أي دين، وتم تعليق المراسلة حتى تراجعها الإدارة.','success')
            return redirect(url_for('debt_customer_duplicates'))
        customers=db.execute('''SELECT * FROM debt_customers WHERE merged_into_customer_id IS NULL ORDER BY id DESC''').fetchall()
        groups={}
        for c in customers:
            keys=[]
            if c['normalized_phone']:keys.append('P:'+c['normalized_phone'])
            if len(c['normalized_name'] or '')>=3:keys.append('N:'+c['normalized_name'])
            for key in keys:groups.setdefault(key,[]).append(c)
        seen=set();dup_groups=[]
        for key,rs in groups.items():
            if len(rs)<2:continue
            ids=tuple(sorted(r['id'] for r in rs))
            if ids in seen:continue
            seen.add(ids)
            cards=[]
            for c in rs:
                stats=db.execute('''SELECT COUNT(*) AS count,COALESCE(SUM(remaining_amount),0) AS balance
                  FROM debts_ledger WHERE customer_id=?''',(c['id'],)).fetchone()
                cards.append({'id':c['id'],'name':c['name'],'mobile':c['mobile'],
                              'count':stats['count'],'balance':stats['balance']})
            dup_groups.append({'reason':('تطابق الجوال' if key.startswith('P:') else 'تطابق الاسم'), 'customers':cards})
        return render_template('debt_customer_duplicates.html',groups=dup_groups)

    @app.route('/accounting/debts/customers',methods=['GET'])
    def debt_customer_index():
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not any(authorized(es) for es in ('ES1','ES2','ES3')):
            return render_template('no_permission.html'),403
        db=db_ready();q=(request.args.get('q') or '').strip();name=normalized_name(q);phone=normalized_phone(q)
        query='''SELECT c.id,c.name,c.mobile,
             COUNT(d.id) AS debts,COALESCE(SUM(d.remaining_amount),0) AS balance,
             GROUP_CONCAT(DISTINCT d.es) AS branches
             FROM debt_customers c JOIN debts_ledger d ON d.customer_id=c.id
             WHERE c.merged_into_customer_id IS NULL'''
        clauses=[];params=[]
        allowed=[es for es in ('ES1','ES2','ES3') if authorized(es)]
        placeholders=','.join(['?']*len(allowed));clauses.append('d.es IN ('+placeholders+')');params.extend(allowed)
        if q:
            clauses.append('(c.normalized_name LIKE ? OR c.normalized_phone LIKE ? OR d.customer_name LIKE ? OR d.mobile LIKE ?)')
            params += ['%'+name+'%','%'+(phone or q)+'%','%'+q+'%','%'+q+'%']
        query+=' AND '+' AND '.join(clauses)+' GROUP BY c.id ORDER BY c.name LIMIT 250'
        customers=db.execute(query,params).fetchall()
        return render_template('debt_customers_index.html',customers=customers,q=q,is_admin=is_admin())

    # Named helper returned for integration into existing add route.
    return {'setup':db_ready, 'display_matches':display_matches,
            'create_customer':create_customer, 'is_admin':is_admin}
