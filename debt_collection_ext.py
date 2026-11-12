# -*- coding: utf-8 -*-
"""Debt follow-up extension for Al Eshan; no direct message transmission.
The main WhatsApp inbox remains responsible for provider-approved sending.
"""
from datetime import datetime, timezone, timedelta
from flask import request, session, redirect, url_for, render_template, flash
import json
import uuid
import hmac
from urllib.parse import quote
from debt_customers_ext import ensure_customer_schema, normalized_phone


def debt_message_decision(row, policy):
    """Single, customer-level administrative policy for every debt row.

    Only a positive, documented opt-in gives access to the direct WhatsApp
    link; every other administrative decision replaces the send action.
    Final sending still depends on WhatsApp's platform rules and window.
    """
    def state(label, can_send=False, tone='secondary'):
        return {'label': label, 'can_send': can_send, 'tone': tone}

    if float(row['remaining_amount'] or 0) <= 0:
        return state('تم السداد — لا توجد مديونية', tone='secondary')
    if policy and int(policy['dispute'] or 0):
        return state('إيقاف المراسلة — اعتراض على الدين', tone='danger')
    if not policy:
        return state('بانتظار قرار الإدارة', tone='warning')
    policy_name = str(policy['policy'] or 'pending')
    if policy_name == 'blocked':
        return state('قرار الإدارة: ممنوع التواصل', tone='danger')
    if policy_name == 'call':
        return state('قرار الإدارة: اتصال فقط', tone='warning')
    if policy_name == 'internal':
        return state('قرار الإدارة: متابعة داخلية فقط', tone='secondary')
    if policy_name == 'paused':
        until = str(policy['pause_until'] or '').strip()
        return state('قرار الإدارة: المراسلة مؤجلة' + (' حتى ' + until if until else ''), tone='warning')
    if policy_name != 'allow':
        return state('بانتظار قرار الإدارة', tone='warning')
    if not policy['consent_at'] or not str(policy['consent_source'] or '').strip():
        return state('بانتظار توثيق موافقة العميل', tone='warning')
    return state('إرسال رسالة', can_send=True, tone='success')


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
        db.execute('''CREATE TABLE IF NOT EXISTS debt_manual_whatsapp_open (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debt_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            wa_number_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            message_snapshot TEXT NOT NULL,
            opened_by TEXT NOT NULL,
            opened_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        # Keep historical open-attempt rows; migrate their optional confirmation data in place.
        manual_cols = {c[1] for c in db.execute('PRAGMA table_info(debt_manual_whatsapp_open)').fetchall()}
        for column, kind in (
            ('status', "TEXT NOT NULL DEFAULT 'opened'"),
            ('confirmed_sent_at','TEXT'), ('confirmed_by','TEXT'),
            ('cancelled_at','TEXT'), ('cancelled_by','TEXT'),
            ('included_branches','TEXT'),
        ):
            if column not in manual_cols:
                db.execute(f'ALTER TABLE debt_manual_whatsapp_open ADD COLUMN {column} {kind}')
        db.execute('CREATE INDEX IF NOT EXISTS idx_debt_manual_open_customer ON debt_manual_whatsapp_open(customer_id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_debt_manual_status ON debt_manual_whatsapp_open(customer_id,status,id)')
        db.execute('''CREATE TABLE IF NOT EXISTS wa_manual_browser_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_number_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            contact_name TEXT,
            message_snapshot TEXT,
            opened_by TEXT NOT NULL,
            opened_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'opened',
            confirmed_at TEXT,
            confirmed_by TEXT,
            cancelled_at TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS wa_manual_sync_links (
            source_kind TEXT NOT NULL,
            source_attempt_id INTEGER NOT NULL,
            wa_message_id INTEGER NOT NULL,
            PRIMARY KEY (source_kind,source_attempt_id)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_wa_manual_outbox_conv ON wa_manual_browser_outbox(conversation_id,status)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_debt_followups_debt ON debt_followups(debt_id)')
        db.commit()

    def _confirm_token():
        if not session.get('_debt_send_confirm_token'):
            session['_debt_send_confirm_token'] = uuid.uuid4().hex
        return session['_debt_send_confirm_token']

    def _verify_confirm_token():
        stored = session.get('_debt_send_confirm_token') or ''
        given = request.form.get('confirm_token') or ''
        return bool(stored and given and hmac.compare_digest(stored, given))

    def _riyadh_time(raw):
        if not raw:
            return '-'
        try:
            value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone(timedelta(hours=3)))
            return value.strftime('%Y-%m-%d %I:%M %p').replace('AM','ص').replace('PM','م')
        except (ValueError, TypeError):
            return str(raw)

    app.add_template_filter(_riyadh_time, 'debt_riyadh')

    @app.context_processor
    def _manual_debt_send_context():
        # Only initialize this on debt ledger pages, not every application page.
        if not session.get('logged_in') or request.endpoint not in ('accounting_debts','accounting_debts_branch'):
            return {}
        db = get_db()
        setup(db)
        current_user = session.get('username') or ''
        pending = db.execute("""SELECT customer_id, COUNT(*) AS c
            FROM debt_manual_whatsapp_open
            WHERE status='opened' AND opened_by=? GROUP BY customer_id""",
            (current_user,)).fetchall()
        latest = db.execute("""SELECT customer_id, MAX(confirmed_sent_at) AS sent_at
            FROM debt_manual_whatsapp_open WHERE status='staff_confirmed'
            GROUP BY customer_id""").fetchall()
        return {
            'debt_confirm_token': _confirm_token(),
            'debt_pending_by_customer': {int(x['customer_id']): int(x['c']) for x in pending},
            'debt_last_confirmed_by_customer': {int(x['customer_id']): _riyadh_time(x['sent_at']) for x in latest},
        }

    @app.route('/accounting/debts/<es>/confirm-manual/<int:debt_id>', methods=['POST'])
    def debt_confirm_manual_send(es, debt_id):
        """Employee attestation; never claims provider-confirmed delivery."""
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not _verify_confirm_token():
            return 'طلب غير صالح، قم بتحديث الصفحة ثم أعد المحاولة.', 400
        es = (es or '').upper().strip()
        db = get_db()
        setup(db)
        debt = get_case(db, es, debt_id)
        if debt is None:
            return render_template('no_permission.html'), 403
        user = session.get('username') or ''
        action = request.form.get('action') or 'confirm'
        if action not in ('confirm', 'not_sent'):
            return 'إجراء غير صحيح', 400
        raw_id = request.form.get('attempt_id') or ''
        if raw_id:
            try:
                attempt_id = int(raw_id)
            except ValueError:
                return 'رقم سجل غير صالح', 400
            attempt = db.execute("""SELECT * FROM debt_manual_whatsapp_open
                WHERE id=? AND customer_id=? AND status='opened'""",
                (attempt_id, debt['customer_id'])).fetchone()
        else:
            # Ledger quick confirm: only the current employee's newest pending attempt.
            attempt = db.execute("""SELECT * FROM debt_manual_whatsapp_open
                WHERE customer_id=? AND opened_by=? AND status='opened'
                ORDER BY id DESC LIMIT 1""",
                (debt['customer_id'], user)).fetchone()
        if not attempt:
            flash('لا توجد محاولة فتح واتساب بانتظار التأكيد لهذا العميل.', 'warning')
            return redirect(url_for('accounting_debts_branch', es=es))
        if attempt['opened_by'] != user and not admin():
            return render_template('no_permission.html'), 403
        opened_debt = db.execute('SELECT es,customer_id FROM debts_ledger WHERE id=?',
                                 (attempt['debt_id'],)).fetchone()
        if not opened_debt or opened_debt['customer_id'] != debt['customer_id'] or not authorized(opened_debt['es']):
            return render_template('no_permission.html'), 403

        now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
        if action == 'confirm':
            with db:
                changed = db.execute('''UPDATE debt_manual_whatsapp_open
                  SET status='staff_confirmed', confirmed_sent_at=?, confirmed_by=?
                  WHERE id=? AND status='opened' ''',
                  (now_utc,user,attempt['id'])).rowcount
                if changed:
                    try:
                        branches=json.loads(attempt['included_branches'] or '[]')
                    except (ValueError,TypeError):
                        branches=[]
                    # Old open attempts have no branch snapshot; update only their original debt.
                    branches=[br for br in branches if br in ('ES1','ES2','ES3') and authorized(br)]
                    if not branches:
                        branches=[opened_debt['es']]
                    placeholders=','.join('?' for _ in branches)
                    db.execute(f'''UPDATE debts_ledger SET last_sent_at=?
                      WHERE customer_id=? AND es IN ({placeholders})''',
                      [now_utc, debt['customer_id'], *branches])
                    customer = db.execute('SELECT name FROM debt_customers WHERE id=?',
                                          (debt['customer_id'],)).fetchone()
                    _create_local_manual_message(
                        db,'debt',attempt['id'],attempt['wa_number_id'],
                        attempt['destination'],customer['name'] if customer else '',
                        attempt['message_snapshot'],user,now_utc)
            if changed:
                flash('تم توثيق إرسال الرسالة حسب تأكيد الموظف. لا يعني ذلك تأكيد وصولها أو قراءتها في واتساب.', 'success')
        else:
            with db:
                changed=db.execute('''UPDATE debt_manual_whatsapp_open
                  SET status='not_sent', cancelled_at=?, cancelled_by=?
                  WHERE id=? AND status='opened' ''',
                  (now_utc,user,attempt['id'])).rowcount
            if changed:
                flash('تم تسجيل أن المحادثة فُتحت ولكن لم تُرسل منها رسالة.', 'info')
        if request.form.get('return_to') == 'case':
            return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
        return redirect(url_for('accounting_debts_branch',es=es))

    def _manual_source_token():
        if not session.get('_wa_manual_token'):
            session['_wa_manual_token'] = uuid.uuid4().hex
        return session['_wa_manual_token']

    def _manual_source_check():
        token = session.get('_wa_manual_token') or ''
        provided = request.form.get('manual_token') or ''
        return bool(token and provided and hmac.compare_digest(token, provided))

    def _create_local_manual_message(db, source_kind, source_attempt_id,
                                     number_id, destination, customer_name,
                                     body, sent_by, confirmed_at):
        # Make a staff-confirmed *local log* visible in the unified inbox.

        # This is intentionally not a provider acknowledgement or a message send.
        # Never set meta_message_id, delivered/read, or last_inbound_at.
        previous = db.execute('''SELECT wa_message_id FROM wa_manual_sync_links
            WHERE source_kind=? AND source_attempt_id=?''',
            (source_kind, source_attempt_id)).fetchone()
        if previous:
            return previous['wa_message_id']
        number = db.execute('SELECT id, is_active FROM wa_numbers WHERE id=?',
                            (number_id,)).fetchone()
        if not number or not number['is_active']:
            # Do not erase the attestation. The number may have been disabled.
            return None
        destination = normalize_phone(destination)
        if not destination:
            return None
        conv = db.execute('''SELECT id FROM wa_conversations
            WHERE wa_number_id=? AND contact_wa_id=? AND is_deleted=0
            ORDER BY id DESC LIMIT 1''', (number_id,destination)).fetchone()
        if conv:
            conversation_id = conv['id']
        else:
            c = db.execute('''INSERT INTO wa_conversations
                (wa_number_id,contact_wa_id,contact_name,last_message,last_message_at,
                 last_inbound_at,unread_count,updated_at)
                 VALUES (?,?,?,NULL,NULL,NULL,0,?)''',
                 (number_id,destination,customer_name or destination,confirmed_at))
            conversation_id = c.lastrowid
        # If a genuine Coexistence echo has already arrived, use it rather
        # than creating a second local copy. Later echoes may still be shown
        # separately, and their actual provider status remains authoritative.
        provider_row = None
        if body:
            # Do not match a message from weeks or months ago just because
            # the text is identical. Only reuse a nearby provider echo.
            matches = db.execute('''SELECT m.id,m.sent_at FROM wa_messages m
                WHERE m.conversation_id=? AND m.direction='out'
                  AND m.meta_message_id IS NOT NULL AND m.body=?
                ORDER BY m.id DESC LIMIT 25''', (conversation_id,body)).fetchall()
            try:
                when = datetime.fromisoformat(str(confirmed_at).replace('Z','+00:00'))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError,ValueError):
                when = None
            for candidate in matches:
                try:
                    actual = datetime.fromisoformat(str(candidate['sent_at']).replace('Z','+00:00'))
                    if actual.tzinfo is None:
                        actual = actual.replace(tzinfo=timezone.utc)
                    if when and abs((actual-when).total_seconds()) <= 7200:
                        provider_row = candidate
                        break
                except (TypeError,ValueError):
                    continue
        if provider_row:
            message_id = provider_row['id']
        else:
            display_body = body or 'رسالة من واتساب ويب: أكد الموظف إرسالها دون حفظ نصها.'
            cur = db.execute('''INSERT INTO wa_messages
                (conversation_id,meta_message_id,direction,message_type,body,sent_at,
                 status,status_error,sent_by_user_id,sent_by_username,created_at)
                VALUES (?,NULL,'out','manual_web',?,?,'staff_confirmed',
                 'توثيق يدوي من الموظف؛ لم يؤكده واتساب ولا يثبت وصول الرسالة',NULL,?,?)''',
                 (conversation_id,display_body,confirmed_at,sent_by,confirmed_at))
            message_id = cur.lastrowid
        db.execute('''INSERT OR IGNORE INTO wa_manual_sync_links
            (source_kind,source_attempt_id,wa_message_id) VALUES (?,?,?)''',
            (source_kind,source_attempt_id,message_id))
        db.execute('''UPDATE wa_conversations
            SET last_message=?,last_message_at=?,updated_at=?
            WHERE id=? AND (last_message_at IS NULL OR last_message_at <= ?)''',
            ((body or 'توثيق إرسال يدوي من واتساب ويب')[:300],confirmed_at,confirmed_at,
             conversation_id,confirmed_at))
        return message_id

    def _backfill_confirmed_debt_messages(db):
        rows = db.execute('''SELECT a.*,c.name AS customer_name
            FROM debt_manual_whatsapp_open a
            LEFT JOIN debt_customers c ON c.id=a.customer_id
            WHERE a.status='staff_confirmed' AND NOT EXISTS (
              SELECT 1 FROM wa_manual_sync_links l
              WHERE l.source_kind='debt' AND l.source_attempt_id=a.id)
            ORDER BY a.id DESC LIMIT 100''').fetchall()
        if not rows:
            return
        with db:
            for a in rows:
                _create_local_manual_message(db,'debt',a['id'],a['wa_number_id'],
                    a['destination'],a['customer_name'],a['message_snapshot'],
                    a['confirmed_by'] or a['opened_by'],
                    a['confirmed_sent_at'] or a['opened_at'])

    @app.context_processor
    def _wa_manual_context():
        if not session.get('logged_in') or request.endpoint != 'whatsapp_branches':
            return {}
        db = get_db()
        setup(db)
        _backfill_confirmed_debt_messages(db)
        rows = db.execute('''SELECT id,conversation_id,opened_by,message_snapshot
            FROM wa_manual_browser_outbox WHERE status='opened'
              AND opened_by=? ORDER BY id DESC''',
              (session.get('username') or '',)).fetchall()
        pending = {}
        for r in rows:
            pending.setdefault(int(r['conversation_id']),r)
        return {'wa_manual_token':_manual_source_token(),
                'wa_manual_pending': pending}

    @app.route('/whatsapp/manual/open', methods=['POST'])
    def wa_manual_open():
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not _manual_source_check():
            return 'طلب غير صالح، حدّث الصفحة.', 400
        db = get_db()
        setup(db)
        try:
            number_id = int(request.form.get('wa_number_id') or '0')
        except (ValueError,TypeError):
            return 'رقم المؤسسة غير صالح', 400
        if number_id not in wa_allowed_ids(db,require_reply=True):
            return render_template('no_permission.html'), 403
        number = db.execute('SELECT * FROM wa_numbers WHERE id=? AND is_active=1',
                            (number_id,)).fetchone()
        if not number:
            return 'رقم المؤسسة غير متاح', 400
        phone = normalize_phone(request.form.get('phone'))
        if not phone or phone == normalize_phone(number['phone_number']):
            return 'رقم جوال العميل غير صالح', 400
        name = (request.form.get('contact_name') or '').strip()[:120]
        body = (request.form.get('message') or '').strip()[:4000]
        user = session.get('username') or ''
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with db:
            conv = db.execute('''SELECT id FROM wa_conversations
                WHERE wa_number_id=? AND contact_wa_id=? AND is_deleted=0
                ORDER BY id DESC LIMIT 1''',(number_id,phone)).fetchone()
            if conv:
                conversation_id=conv['id']
            else:
                c=db.execute('''INSERT INTO wa_conversations
                   (wa_number_id,contact_wa_id,contact_name,last_message,
                    last_message_at,last_inbound_at,unread_count,updated_at)
                   VALUES (?,?,?,'فتح واتساب ويب — بانتظار تأكيد الإرسال',
                   NULL,NULL,0,?)''',(number_id,phone,name or phone,now))
                conversation_id=c.lastrowid
            db.execute('''INSERT INTO wa_manual_browser_outbox
                (wa_number_id,conversation_id,destination,contact_name,
                 message_snapshot,opened_by,opened_at)
                VALUES (?,?,?,?,?,?,?)''',
                (number_id,conversation_id,phone,name,body,user,now))
        return redirect('https://wa.me/' + phone +
                        ('?text=' + quote(body,safe='') if body else ''),code=302)

    @app.route('/whatsapp/manual/confirm/<int:attempt_id>', methods=['POST'])
    def wa_manual_confirm(attempt_id):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if not _manual_source_check():
            return 'طلب غير صالح، حدّث الصفحة.', 400
        db=get_db()
        setup(db)
        attempt=db.execute('SELECT * FROM wa_manual_browser_outbox WHERE id=?',
                           (attempt_id,)).fetchone()
        if not attempt:
            return 'سجل غير موجود', 404
        if attempt['opened_by'] != (session.get('username') or '') and not admin():
            return render_template('no_permission.html'), 403
        if attempt['wa_number_id'] not in wa_allowed_ids(db,require_reply=True):
            return render_template('no_permission.html'), 403
        if attempt['status'] != 'opened':
            return redirect(url_for('whatsapp_branches',conversation=attempt['conversation_id']))
        action=(request.form.get('action') or 'confirm')
        if action not in ('confirm','not_sent'):
            return 'إجراء غير صالح',400
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with db:
            if action=='confirm':
                changed=db.execute('''UPDATE wa_manual_browser_outbox
                    SET status='staff_confirmed',confirmed_by=?,confirmed_at=?
                    WHERE id=? AND status='opened' ''',
                    (session.get('username'),now,attempt_id)).rowcount
                if changed:
                    _create_local_manual_message(db,'new',attempt_id,
                        attempt['wa_number_id'],attempt['destination'],
                        attempt['contact_name'],attempt['message_snapshot'],
                        session.get('username') or '',now)
            else:
                db.execute('''UPDATE wa_manual_browser_outbox
                    SET status='not_sent',cancelled_at=?
                    WHERE id=? AND status='opened' ''',(now,attempt_id))
        flash('تم حفظ توثيق الموظف في لوحة واتساب، وليس إثباتًا لوصول الرسالة.'
              if action=='confirm' else 'تم تسجيل أنها لم تُرسل.',
              'success' if action=='confirm' else 'info')
        return redirect(url_for('whatsapp_branches',conversation=attempt['conversation_id']))

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
        canonical = db.execute('SELECT name FROM debt_customers WHERE id=?',
                               (row['customer_id'],)).fetchone()
        display_name = canonical['name'] if canonical else row['customer_name']
        if message_builder:
            return message_builder(display_name,total,details)
        return f"عميلنا العزيز {display_name}، المبلغ المستحق {total:.2f} ريال."

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

    def _open_approved_whatsapp_chat(db, es, row, debt_id, error_target):
        """Open the preconfigured WhatsApp chat directly, without sending.

        Reused by both the ledger's Send Message action and the optional
        button on the collection follow-up page.
        """
        p = db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(row['customer_id'],)).fetchone()
        decision = debt_message_decision(row, p)
        if not decision['can_send']:
            flash(decision['label'] + ' — لا يمكن فتح رسالة تحصيل لهذا العميل.', 'warning')
            return redirect(error_target)
        sender = db.execute('''SELECT n.* FROM debt_collection_settings s
            JOIN wa_numbers n ON n.id=s.wa_number_id
            WHERE s.id=1 AND n.is_active=1''').fetchone()
        if not sender:
            flash('حدد رقم واتساب التحصيل أولًا.', 'danger')
            return redirect(error_target)
        nid = int(sender['id'])
        if nid not in wa_allowed_ids(db,require_reply=True):
            flash('ليس لديك صلاحية الرد من رقم التحصيل المحدد.', 'danger')
            return redirect(error_target)
        # After a merge, historical invoices can contain an old phone spelling
        # or an obsolete number. Outbound communication uses the verified
        # canonical customer contact, never the historical invoice field.
        canonical = db.execute('SELECT mobile FROM debt_customers WHERE id=?',
                               (row['customer_id'],)).fetchone()
        phone = normalize_phone(canonical['mobile']) if canonical else ''
        if not phone or phone == normalize_phone(sender['phone_number']):
            flash('رقم العميل غير صالح أو مطابق لرقم المؤسسة.', 'danger')
            return redirect(error_target)
        conversation = db.execute('''SELECT id FROM wa_conversations
           WHERE wa_number_id=? AND contact_wa_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1''',(nid,phone)).fetchone()
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
    @app.route('/accounting/debts/<es>/whatsapp-direct/<int:debt_id>')
    def debt_whatsapp_direct(es, debt_id):
        '''Open WhatsApp Web / Business for manual sending, not via the API.
        This does not prove delivery or control the logged-in sender account.
        '''
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        es = (es or '').strip().upper()
        db = get_db()
        setup(db)
        row = get_case(db, es, debt_id)
        if row is None:
            return render_template('no_permission.html'), 403

        policy = db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',
                            (row['customer_id'],)).fetchone()
        decision = debt_message_decision(row, policy)
        if not decision['can_send']:
            flash(decision['label'] + ' — لا يمكن فتح رسالة تحصيل لهذا العميل.', 'warning')
            return redirect(url_for('accounting_debts_branch', es=es))

        sender = db.execute('''SELECT n.id, n.phone_number, n.label FROM debt_collection_settings s
            JOIN wa_numbers n ON n.id=s.wa_number_id
            WHERE s.id=1 AND n.is_active=1''').fetchone()
        if sender is None:
            flash('اختر رقم واتساب التحصيل من إعدادات الإدارة.', 'danger')
            return redirect(url_for('accounting_debts_branch', es=es))
        if int(sender['id']) not in wa_allowed_ids(db, require_reply=True):
            flash('ليس لديك صلاحية استخدام رقم واتساب التحصيل.', 'danger')
            return redirect(url_for('accounting_debts_branch', es=es))

        canonical = db.execute('SELECT mobile FROM debt_customers WHERE id=?',
                               (row['customer_id'],)).fetchone()
        phone = normalize_phone(canonical['mobile']) if canonical else ''
        if not phone or phone == normalize_phone(sender['phone_number']):
            flash('رقم العميل غير صالح أو مطابق لرقم المؤسسة.', 'danger')
            return redirect(url_for('accounting_debts_branch', es=es))

        message = prepared_for_customer(db, row)
        accessible = [r['es'] for r in db.execute(
            'SELECT DISTINCT es FROM debts_ledger WHERE customer_id=? AND remaining_amount>0',
            (row['customer_id'],)).fetchall() if authorized(r['es'])]
        with db:
            db.execute('''INSERT INTO debt_manual_whatsapp_open
              (debt_id, customer_id, wa_number_id, destination, message_snapshot,
               opened_by, included_branches)
              VALUES (?,?,?,?,?,?,?)''',
              (debt_id, row['customer_id'], int(sender['id']), phone,
               message, session.get('username') or '',json.dumps(accessible)))
        # WhatsApp Business App / Web link; user must review and tap Send.
        # Cannot force a particular sender account via a wa.me link.
        return redirect('https://wa.me/' + phone + '?text=' + quote(message, safe=''), code=302)

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
            return _open_approved_whatsapp_chat(
                db, es, row, debt_id,
                url_for('debt_collection_case', es=es, debt_id=debt_id)
            )
        if request.method == 'POST':
            return redirect(url_for('debt_collection_case',es=es,debt_id=debt_id))
        policy=db.execute('SELECT * FROM debt_customer_policy WHERE customer_id=?',(row['customer_id'],)).fetchone()
        settings=db.execute('''SELECT n.id,n.label,n.phone_number FROM debt_collection_settings s
          LEFT JOIN wa_numbers n ON n.id=s.wa_number_id WHERE s.id=1''').fetchone()
        followups=db.execute('SELECT * FROM debt_followups WHERE debt_id=? ORDER BY id DESC LIMIT 100',(debt_id,)).fetchall()
        opened=db.execute('''SELECT o.*,n.label FROM debt_wa_open_log o LEFT JOIN wa_numbers n
          ON n.id=o.wa_number_id WHERE o.debt_id=? ORDER BY o.id DESC LIMIT 100''',(debt_id,)).fetchall()
        canonical=db.execute('SELECT name,mobile FROM debt_customers WHERE id=?',
                             (row['customer_id'],)).fetchone()
        messages=[]
        if settings and settings['id']:
            phone=normalize_phone(canonical['mobile']) if canonical else ''
            if phone:
                messages=db.execute('''SELECT m.sent_at,m.direction,m.status,m.body,m.sent_by_username
                FROM wa_messages m JOIN wa_conversations c ON c.id=m.conversation_id
                WHERE c.wa_number_id=? AND c.contact_wa_id=? ORDER BY m.id DESC LIMIT 100''',
                (settings['id'],phone)).fetchall()
        related = db.execute('''SELECT id,es,remaining_amount,invoice_date FROM debts_ledger
          WHERE customer_id=? ORDER BY id DESC''',(row['customer_id'],)).fetchall()
        related = [x for x in related if admin() or bool((session.get('perms') or {}).get('debts_'+str(x['es']).lower()))]
        manual_attempts = db.execute("""SELECT m.*, n.label AS source_label, d.es AS original_branch
            FROM debt_manual_whatsapp_open m
            LEFT JOIN wa_numbers n ON n.id=m.wa_number_id
            JOIN debts_ledger d ON d.id=m.debt_id
            WHERE m.customer_id=? ORDER BY m.id DESC LIMIT 100""",
            (row['customer_id'],)).fetchall()
        manual_attempts = [x for x in manual_attempts if authorized(x['original_branch'])]
        return render_template('debt_collection_case.html',row=row,es=es,policy=policy,
          settings=settings,followups=followups,opened=opened,messages=messages,
          related=related,related_total=sum(float(x['remaining_amount'] or 0) for x in related),
          prepared_message=prepared_for_customer(db,row),
          manual_attempts=manual_attempts,
          debt_confirm_token=_confirm_token(), current_username=session.get('username') or '',
          customer_display_name=canonical['name'] if canonical else row['customer_name'],
          decision=debt_message_decision(row, policy), is_admin=admin())
