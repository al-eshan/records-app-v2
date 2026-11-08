/* Al Eshan bilingual UI layer. Only exact, known UI strings are translated.
   Free-text business records, customer messages, and numerical values are never auto-translated.
   For full localization, migrate remaining strings to server-side gettext translations. */
(function () {
  'use strict';
  const KEY = 'eshan_ui_language';
  const pairList = [
    ['الرئيسية','Home'],['المحاسبة المالية','Financial Accounting'],['المحاسبة اليومية','Daily Accounting'],
    ['السجلات','Records'],['الموظفين','Employees'],['الموظفون','Employees'],['المهام','Tasks'],
    ['الاقتراحات','Suggestions'],['روابط مهمة','Important Links'],['الصلاحيات','Permissions'],
    ['لوحة المتابعة','Dashboard'],['واتساب الفروع','Branch WhatsApp'],['تنبيهات المحاسبة','Accounting Alerts'],
    ['خروج','Log out'],['القائمة','Menu'],['رجوع','Back'],['العودة','Back'],
    ['حفظ','Save'],['حفظ التغييرات','Save Changes'],['إلغاء','Cancel'],['حذف','Delete'],
    ['تعديل','Edit'],['إضافة','Add'],['إضافة صف','Add Row'],['إضافة جديد','Add New'],
    ['بحث','Search'],['بحث...','Search...'],['استعلام','Inquiry'],['عرض','View'],['تفاصيل','Details'],
    ['إرسال','Send'],['إرسال رسالة','Send Message'],['محادثة جديدة','New Chat'],
    ['الرسائل','Messages'],['المحادثات','Conversations'],['رسالة جديدة','New Message'],
    ['رقم الجوال','Mobile Number'],['رقم الهاتف','Phone Number'],['رقم العميل','Customer Number'],
    ['اسم العميل','Customer Name'],['العميل','Customer'],['العملاء','Customers'],
    ['الفروع','Branches'],['الفرع','Branch'],['اسم الفرع','Branch Name'],
    ['الحالة','Category'],['النوع','Description'],['المبلغ','Amount'],['الإجمالي','Total'],
    ['التاريخ','Date'],['من تاريخ','From Date'],['إلى تاريخ','To Date'],
    ['صندوق بداية اليوم','Opening Cash'],['صندوق نهاية اليوم','Closing Cash'],
    ['الإجمالي الكلي في إنجاز','Enjaz Grand Total'],['إجمالي صندوق بداية اليوم','Total Opening Cash'],
    ['إجمالي صندوق نهاية اليوم','Total Closing Cash'],['إجمالي فرق الصندوق','Total Cash Difference'],
    ['إجمالي الكاش المستلم','Total Cash Received'],['إجمالي المشتريات النقدية','Total Cash Purchases'],
    ['إجمالي المرتجعات النقدية','Total Cash Returns'],['إجمالي الحوالات البنكية','Total Bank Transfers'],
    ['إجمالي مدفوعات الشبكة mada','Total Mada Card Payments'],
    ['إجمالي مدفوعات تمارا وإمكان','Total Tamara and Emkan Payments'],
    ['إجمالي المدفوعات الآجلة وسدادها','Total Credit and Debt Payments'],
    ['إجمالي المصروفات النثرية','Total Petty Expenses'],['الإجمالي الكلي','Grand Total'],
    ['فرق الصندوق','Cash Difference'],['الخطأ','Difference'],['الصندوق','Cash Register'],
    ['المشتريات النقدية','Cash Purchases'],['المرتجعات النقدية','Cash Returns'],
    ['الدين','Debt'],['سداد الدين','Debt Payment'],['تمارا','Tamara'],['إمكان','Emkan'],
    ['مصروفات نثرية','Petty Expenses'],['المصروفات النثرية','Petty Expenses'],
    ['مدفوعات الشبكة مدى','Mada Card Payments'],['الحوالات البنكية','Bank Transfers'],
    ['الكاش المستلم','Cash Received'],['المدفوعات الآجلة','Credit Payments'],
    ['اعتماد الإدارة المالية','Financial Management Approval'],
    ['تمت التسوية بنجاح','Settlement Completed Successfully'],
    ['مرفوض وتحت المعالجة','Rejected – Under Review'],['ملاحظات (اختياري)','Notes (Optional)'],
    ['ملاحظات','Notes'],['ملاحظات اليوم...','Daily notes...'],
    ['اختيار','Select'],['اختر','Select'],['اختر الحالة','Select Category'],
    ['عنوان','Title'],['الوصف','Description'],['القيمة','Value'],['العدد','Count'],
    ['المتبقي','Remaining'],['المدفوع','Paid'],['المستحق','Due'],['الإجمالي المستحق','Total Due'],
    ['اسم الموظف','Employee Name'],['اسم المستخدم','Username'],['كلمة المرور','Password'],
    ['تسجيل الدخول','Log In'],['دخول','Log In'],['تسجيل خروج','Log out'],
    ['المدير','Manager'],['الإدارة','Management'],['الفرع الأول','Branch One'],
    ['الفرع الثاني','Branch Two'],['الفرع الثالث','Branch Three'],
    ['العمليات','Transactions'],['جدول العمليات','Transactions Table'],
    ['الإجراءات','Actions'],['تفاصيل العملية','Transaction Details'],
    ['الموافقات','Approvals'],['اعتماد','Approve'],['رفض','Reject'],
    ['إرسال بطاقة حفظ الرقم للعميل','Send Contact Card to Customer'],
    ['أرقام واتساب','WhatsApp Numbers'],['إدارة أرقام واتساب','Manage WhatsApp Numbers'],
    ['صلاحيات الموظفين على أرقام واتساب','Employee WhatsApp Number Permissions'],
    ['جميع الفروع','All Branches'],['تحديث','Refresh'],['إعادة تحميل','Reload'],
    ['السابق','Previous'],['التالي','Next'],['تصفية','Filter'],['مسح','Clear'],
    ['تصدير Excel','Export Excel'],['طباعة','Print'],['إجمالي الديون','Total Debts'],
    ['دفتر الديون','Debt Ledger'],['حسابات يومية','Daily Accounts'],
    ['الالتزامات المالية','Financial Commitments'],['عمولة المبيعات','Sales Commission']
  ];
  const translations = new Map(pairList);
  const originals = new WeakMap();
  const attrOriginals = new WeakMap();
  let language = 'ar';
  let scheduled = false;
  function getDesired() {try {return localStorage.getItem(KEY)==='en'?'en':'ar';}catch(e){return 'ar';}}
  function saveDesired(v) {try {localStorage.setItem(KEY,v);}catch(e){}}
  function translateString(v) {
    const trimmed=v.trim();
    if (!trimmed) return v;
    const direct=translations.get(trimmed);
    if (direct) return v.replace(trimmed,direct);
    // Handle menu emoji, preserving icons and whitespace.
    const emoji=/^([^\p{L}\p{N}]*)([\s\S]*?)$/u.exec(trimmed);
    if (emoji) {
      const phrase=emoji[2].trim();
      const t=translations.get(phrase);
      if (t) return v.replace(trimmed,emoji[1]+t);
    }
    return v;
  }
  function excluded(el) {return !el || !!el.closest('[data-no-translate], .whatsapp-message, .message-bubble, .chat-bubble, .chat-message, pre, code, textarea, [contenteditable]');}
  function translateNode(node) {
    if (excluded(node.parentElement)) return;
    if (!originals.has(node)) originals.set(node,node.nodeValue);
    const original=originals.get(node);
    const next=language==='en'?translateString(original):original;
    if (node.nodeValue!==next) node.nodeValue=next;
  }
  function translateAttrs(el) {
    if (excluded(el)) return;
    const attrs=['placeholder','title','aria-label'];
    let old=attrOriginals.get(el);
    if (!old) {old={}; attrOriginals.set(el,old);}
    for (const a of attrs) {
      if (!el.hasAttribute(a)) continue;
      if (!(a in old)) old[a]=el.getAttribute(a);
      const next=language==='en'?translateString(old[a]):old[a];
      if (el.getAttribute(a)!==next) el.setAttribute(a,next);
    }
  }
  function updateUI() {
    document.documentElement.lang=language;
    document.documentElement.dir=language==='ar'?'rtl':'ltr';
    document.querySelectorAll('[data-lang-choice]').forEach(btn=>btn.setAttribute('aria-pressed',String(btn.dataset.langChoice===language)));
    const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    let n;
    while ((n=walk.nextNode())) translateNode(n);
    document.querySelectorAll('[placeholder],[title],[aria-label]').forEach(translateAttrs);
  }
  function schedule() {if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;updateUI();});}
  function start() {
    language=getDesired();
    document.querySelectorAll('[data-lang-choice]').forEach(btn=>btn.addEventListener('click',()=>{
      language=btn.dataset.langChoice==='en'?'en':'ar';saveDesired(language);schedule();
    }));
    updateUI();
    const observer=new MutationObserver(records=>{
      // Refresh newly appended UI rows, menus and popups while preserving already translated labels.
      if(records.some(r=>r.type==='childList' && r.addedNodes.length)) schedule();
    });
    observer.observe(document.body,{subtree:true,childList:true});
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',start,{once:true});
  else start();
})();
