# -*- coding: utf-8 -*-
"""Debt follow-up extension for Al Eshan; no direct message transmission.
The main WhatsApp inbox remains responsible for provider-approved sending.
"""
from datetime import datetime, timezone
from flask import request, session, redirect, url_for, render_template, flash
import json
from debt_customers_ext import ensure_customer_schema, normalized_phone


def install_debt_collection(app, get_db, ensure_debts, ensure_whatsapp,
                            wa_allowed_ids, normalize_phone, message_builder=None):
    def admin():
        return session.get('username') == 'adm-es'

    def authorized(es):
        return admin() or bool((session.get('perms') or {}).get('debts_' + es.lower()))

    def setup(db):
        ensure_debts(db)
        ensure_customer_schema(db)
        ensure_whatsapp(db)
        db.execute('''CREATE TABLE IF NOT EXISTS debt_contact_policy (
            debt_id INTEGER PRIMARY KEY, policy TEXT NOT NULL DEFAULT 'pending',
            consent_at TEXT, consent_source TEXT, pause_until TEXT,
            dispute INTEGER NOT NULL DEFAULT 0, note TEXT,
            updated_by TEXT, updated_at TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS debt_policy_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER NOT NULL,
            old_values TEXT, new_values TEXT, changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT (datetime('now')))''')
        db.execute('''CREATE TABLE IF NOT EXISTS debt_followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER NOT NULL,
            action_type TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            followup_date TEXT, promised_amount REAL, performed_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))''')
        db.execute('''CREATE TABLE IF NOT EXISTS debt_collection_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), wa_number_id INTEGER,
            updated_by TEXT, updated_at TEXT)''')
        db.execute('INSERT OR IGNORE INTO debt_collection_settings(id) VALUES (1)')
        db.execute('''CREATE TABLE IF NOT EXISTS debt_wa_open_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER NOT NULL,
            wa_number_id INTEGER NOT NULL, conversation_id INTEGER NOT NULL,
            opened_by TEXT NOT NULL,
            opened_at TEXT NOT NULL DEFAULT (datetime('now')))''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_debt_followups_debt ON debt_followups(debt_id)')
        db.commit()

    def get_case(db, es, debt_id):
        if es not in ('ES1', 'ES2', 'ES3') or not authorized(es):
            return None
        return db.execute('SELECT * FROM debts_ledger WHERE id=? AND es=?',
                          (debt_id, es)).fetchone()

    def prepared_for_customer(db, row):
        # Includes only customer debts in branches this user may access.
        items = db.execute('''SELECT es,remaining_amount FROM debts_ledger
          WHERE customer_id=?''',(row['customer_id'],)).fetchall()
        amounts = {}
        for item in items:
            es_code = item['es']
            if admin() or bool((session.get('perms') or {}).get('debts_' + es_code.lower())):
                amounts[es_code] = amounts.get(es_code,0.0) + max(0.0,float(item['remaining_amount'] or 0))
        details = sorted((code,round(amount,2)) for code,amount in amounts.items() if amount > 0)
        total = round(sum(amount for _,amount in details),2)
        if message_builder:
            return message_builder(row['customer_name'],total,details)
        return f"عميلنا العزيز {row['customer_name']}، المبلغ المستحق {total:.2f} ريال."

    def safe_date(raw):
        if not raw:
            return None
        datetime.strptime(raw, '%Y-%m-%d')
        return raw

    @app.route('/accounting/debts/communication-settings', methods=['GET', 'POST'])
    def debt_communication_settings():
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not admin():
            return render_template('no_permission.html'), 403
        db = get_db()
        setup(db)
        if request.method == 'POST':
            raw = (request.form.get('wa_number_id') or '').strip()
            nid = None
            if raw:
                try:
                    nid = int(raw)
                except ValueError:
                    nid = -1
                active = db.execute('SELECT id FROM wa_numbers WHERE id=? AND is_active=1', (nid,)).fetchone()
                if not active:
                    flash('اختر رقمًا مفعّلًا من أرقام واتساب المسجلة.', 'danger')
                    return redirect(url_for('debt_communication_settings'))
            db.execute('''UPDATE debt_collection_settings SET wa_number_id=?,
                          updated_by=?, updated_at=datetime('now') WHERE id=1''',
                       (nid, session.get('username')))
            db.commit()
            flash('تم حفظ رقم واتساب التحصيل بصورة دائمة.', 'success')
            return redirect(url_for('debt_communication_settings'))
        numbers = db.execute('SELECT * FROM wa_numbers ORDER BY sort_order,id').fetchall()
        settings = db.execute('SELECT * FROM debt_collection_settings WHERE id=1').fetchone()
        return render_template('debt_communication_settings.html', numbers=numbers, settings=settings)

    @app.route('/accounting/debts/<es>/case/<int:debt_id>', methods=['GET', 'POST'])
    def debt_collection_case(es, debt_id):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        es = es.upper()
        db = get_db()
        setup(db)
        row = get_case(db, es, debt_id)
        if row is None:
            return render_template('no_permission.html'), 403
        action = request.form.get('action', '') if request.method == 'POST' else ''
        if action == 'policy':
            if not admin():
                return render_template('no_permission.html'), 403
            policy_name = (request.form.get('policy') or 'pending').strip()
            if policy_name not in ('pending','allow','call','internal','paused','blocked'):
                flash('سياسة غير صحيحة.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            consent = request.form.get('consent_confirmed') == 'on'
            source = (request.form.get('consent_source') or '').strip()[:200]
            pause_raw = (request.form.get('pause_until') or '').strip()
            try:
                pause_until = safe_date(pause_raw)
            except ValueError:
                pause_until = None
                flash('تاريخ التأجيل غير صالح.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            if policy_name == 'paused' and not pause_until:
                flash('حدد تاريخ التأجيل.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            if policy_name == 'allow' and (not consent or not source):
                flash('يجب توثيق موافقة العميل ومصدرها قبل السماح بالمراسلة.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            old = db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(row['customer_id'],)).fetchone()
            old_data = dict(old) if old else {}
            consent_at = (datetime.now(timezone.utc).isoformat(timespec='seconds') if consent else None)
            note = (request.form.get('note') or '').strip()[:500]
            with db:
                db.execute('''INSERT INTO debt_customer_policy
                 (customer_id,policy,consent_at,consent_source,pause_until,note,updated_by,updated_at)
                 VALUES (?,?,?,?,?,?,?,datetime('now'))
                 ON CONFLICT(customer_id) DO UPDATE SET policy=excluded.policy,
                 consent_at=excluded.consent_at,consent_source=excluded.consent_source,
                 pause_until=excluded.pause_until,note=excluded.note,
                 updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
                 (row['customer_id'],policy_name,consent_at,source,pause_until,note,session.get('username')))
                db.execute('''INSERT INTO debt_policy_audit
                 (debt_id,old_values,new_values,changed_by) VALUES (?,?,?,?)''',
                 (debt_id,json.dumps(old_data,ensure_ascii=False),
                  json.dumps({'policy':policy_name,'consent':bool(consent_at),'pause_until':pause_until},ensure_ascii=False),
                  session.get('username') or ''))
            flash('تم تسجيل قرار الإدارة.', 'success')
        elif action == 'followup':
            kind = (request.form.get('action_type') or 'note').strip()
            if kind not in ('note','call','visit','promise','dispute'):
                flash('نوع متابعة غير صالح.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            note = (request.form.get('note') or '').strip()[:1000]
            try:
                next_date = safe_date((request.form.get('followup_date') or '').strip())
                promised = float(request.form.get('promised_amount') or 0)
                if promised < 0 or promised > float(row['remaining_amount'] or 0):
                    raise ValueError()
            except ValueError:
                flash('تاريخ المتابعة أو مبلغ الوعد غير صالح.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            if not note:
                flash('أدخل نتيجة المتابعة.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            with db:
                db.execute('''INSERT INTO debt_followups
                  (debt_id,action_type,note,followup_date,promised_amount,performed_by)
                  VALUES (?,?,?,?,?,?)''',
                  (debt_id,kind,note,next_date,promised or None,session.get('username') or ''))
                if kind == 'dispute':
                    db.execute('INSERT OR IGNORE INTO debt_customer_policy(customer_id) VALUES (?)',(row['customer_id'],))
                    db.execute('UPDATE debt_customer_policy SET dispute=1 WHERE customer_id=?',(row['customer_id'],))
            flash('تم تسجيل المتابعة.', 'success')
        elif action == 'resolve_dispute':
            if not admin():
                return render_template('no_permission.html'), 403
            with db:
                db.execute('UPDATE debt_customer_policy SET dispute=0 WHERE customer_id=?',(row['customer_id'],))
                db.execute('''INSERT INTO debt_followups(debt_id,action_type,note,performed_by)
                 VALUES (?,'note',?,?)''',(debt_id,'رفعت الإدارة تعليق الاعتراض بعد المراجعة.',session.get('username')))
            flash('تم رفع تعليق الاعتراض.', 'success')
        elif action == 'open_chat':
            p = db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(row['customer_id'],)).fetchone()
            if not p or p['policy'] != 'allow' or not p['consent_at'] or p['dispute']:
                flash('المراسلة غير مصرح بها أو المديونية محل اعتراض.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            if float(row['remaining_amount'] or 0) <= 0:
                flash('لا توجد مديونية متبقية.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            sender = db.execute('''SELECT n.* FROM debt_collection_settings s
                JOIN wa_numbers n ON n.id=s.wa_number_id
                WHERE s.id=1 AND n.is_active=1''').fetchone()
            if not sender:
                flash('حدد رقم واتساب التحصيل أولًا.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            nid = int(sender['id'])
            if nid not in wa_allowed_ids(db,require_reply=True):
                flash('ليس لديك صلاحية الرد من رقم التحصيل المحدد.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            phone = normalize_phone(row['mobile'])
            canonical = db.execute('SELECT mobile FROM debt_customers WHERE id=?',(row['customer_id'],)).fetchone()
            canonical_phone = normalize_phone(canonical['mobile']) if canonical else ''
            if not canonical_phone or phone != canonical_phone:
                flash('رقم هذا الدين لا يطابق جوال ملف العميل الموحد؛ راجعه قبل أي مراسلة.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            if not phone or phone == normalize_phone(sender['phone_number']):
                flash('رقم العميل غير صالح أو مطابق لرقم المؤسسة.', 'danger')
                return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
            conversation = db.execute('''SELECT id FROM wa_conversations
               WHERE wa_number_id=? AND contact_wa_id=? ORDER BY id DESC LIMIT 1''',(nid,phone)).fetchone()
            with db:
                if conversation:
                    cid=conversation['id']
                else:
                    c = db.execute('''INSERT INTO wa_conversations
                     (wa_number_id,contact_wa_id,contact_name,last_message,last_message_at,last_inbound_at,unread_count,updated_at)
                     VALUES (?,?,?,'محادثة تحصيل — لم يتم إرسال رسالة بعد',NULL,NULL,0,datetime('now'))''',
                     (nid,phone,row['customer_name']))
                    cid=c.lastrowid
                db.execute('''INSERT INTO debt_wa_open_log
                 (debt_id,wa_number_id,conversation_id,opened_by) VALUES (?,?,?,?)''',
                 (debt_id,nid,cid,session.get('username') or ''))
            # This draft is presented in the inbox, never transmitted automatically.
            session['_debt_wa_draft'] = {'conversation_id':int(cid),
                                         'text':prepared_for_customer(db,row)}
            flash('فُتحت المحادثة مع مسودة الرسالة. لم تُرسل أي رسالة بعد.', 'info')
            return redirect(url_for('whatsapp_branches',conversation=cid,number=nid))
        if request.method == 'POST':
            return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
        policy=db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(row['customer_id'],)).fetchone()
        settings=db.execute('''SELECT n.id,n.label,n.phone_number FROM debt_collection_settings s
          LEFT JOIN wa_numbers n ON n.id=s.wa_number_id WHERE s.id=1''').fetchone()
        followups=db.execute('SELECT * FROM debt_followups WHERE debt_id=? ORDER BY id DESC LIMIT 100',(debt_id,)).fetchall()
        opened=db.execute('''SELECT o.*,n.label FROM debt_wa_open_log o LEFT JOIN wa_numbers n
          ON n.id=o.wa_number_id WHERE o.debt_id=? ORDER BY o.id DESC LIMIT 100''',(debt_id,)).fetchall()
        messages=[]
        if settings and settings['id']:
            phone=normalize_phone(row['mobile'])
            if phone:
                messages=db.execute('''SELECT m.sent_at,m.direction,m.status,m.body,m.sent_by_username
                FROM wa_messages m JOIN wa_conversations c ON c.id=m.conversation_id
                WHERE c.wa_number_id=? AND c.contact_wa_id=? ORDER BY m.id DESC LIMIT 100''',
                (settings['id'],phone)).fetchall()
        related = db.execute('''SELECT id,es,remaining_amount,invoice_date FROM debts_ledger
          WHERE customer_id=? ORDER BY id DESC''',(row['customer_id'],)).fetchall()
        related = [x for x in related if admin() or bool((session.get('perms') or {}).get('debts_'+str(x['es']).lower()))]
        return render_template('debt_collection_case.html',row=row,es=es,policy=policy,
          settings=settings,followups=followups,opened=opened,messages=messages,
          related=related,related_total=sum(float(x['remaining_amount'] or 0) for x in related),
          prepared_message=prepared_for_customer(db,row),is_admin=admin())
