# DYAF v2 — AML/Fraud Transaction Monitoring (SELECT Engine)

منصة مراقبة الحركات المالية (TMS) حيث **كل قاعدة كشف هي جملة SELECT كاملة** —
لا ميزات مخصصة لكل سيناريو: أي سيناريو جديد = استعلام مختلف بنفس المكونات.

```sql
SELECT   <تجميعات مسماة، لكل واحدة FILTER خاص>
FROM     <أي فهرس Elasticsearch>
WHERE    <شروط متداخلة + date math (now-7d) + قوائم @name + حقول محسوبة>
GROUP BY <حقل أو أكثر (كيانات مركبة مثل محفظة×دولة) أو لا شيء (تنبيه لكل سجل)>
HAVING   <تعبير حسابي/منطقي حر بين التجميعات>
```

مثال — «العميل الذي صادره أكبر من وارده»:

```sql
SELECT wallet_id,
       SUM(amount) FILTER (WHERE is_debit = true)  AS debit_sum,
       SUM(amount) FILTER (WHERE is_debit = false) AS credit_sum
FROM   transactions
WHERE  @timestamp >= now-7d
GROUP BY wallet_id
HAVING debit_sum > credit_sum
```

---

## التشغيل السريع

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m pytest tests/ -q     # 39 اختباراً، منها اختبار لكل سيناريو TM
python demo.py                 # يشغّل كتالوج السيناريوهات كاملاً
python -m dyaf.api             # الخادم -> http://127.0.0.1:8000
```

الإعدادات عبر `.env` (انسخ `.env.example`): عنوان Elasticsearch، اسم المستخدم/كلمة
المرور أو API Key، شهادة CA، ومنفذ/عنوان الخادم. بدون `ELASTICSEARCH_URL` يعمل
محاكي ES مدمج تلقائياً للتجربة — الكود لا يتغير مع cluster حقيقي.

---

## نموذج البيانات: قيد مزدوج (Double-Entry)

كل حركة منطقية تُخزَّن **سجلين** في فهرس `transactions`:

| الحقل | جانب المرسل | جانب المستقبل |
|---|---|---|
| `is_debit` | `true` | `false` |
| `wallet_id` | محفظة المرسل | محفظة المستقبل |
| `counterparty_wallet_id` | المستقبل | المرسل |
| `wallet_*` | كل صفات محفظة هذا الجانب (**JOIN** من فهرس المحافظ) | مثله |
| بقية حقول الحركة | مشتركة (المبلغ، الوقت، النوع، المرجع، التاجر...) | مثلها |

بهذا تُجمَّع «كل حركات العميل» بحقل واحد (`wallet_id`)، ويفرز `is_debit`
الصادر عن الوارد — فيصبح debit مقابل credit مجرد FILTER في SELECT.

**JOINs**: لأن Elasticsearch لا يدعم join وقت الاستعلام، تُطبَّق تعريفات join
القابلة للتهيئة وقت الفهرسة: `{source_field, target, prefix}` ضد
wallets / transaction_types / internal_wallets — قابلة للتعديل لكل مصدر.

## الديناميكية

- **مصادر بيانات**: أي فهرس ES يُسجَّل كمصدر من الواجهة (اسم، حقل وقت، حقل معرف،
  أعمدة CSV الإجبارية، joins) — بلا كود.
- **حقول ديناميكية**: تُكتشف من الـ mapping الحي؛ أي عمود CSV إضافي يصبح حقلاً فوراً.
- **حقول محسوبة**: يعرّفها المستخدم بتعبير، مثل `is_round = amount % 1000 == 0` —
  تُقيَّم محلياً وتُترجم إلى Painless runtime fields في استعلام ES.
- **قوائم مسماة**: `@high_risk_countries` تُدار من الواجهة وتُستخدم في أي شرط.
- **قيم منسدلة**: قيمة الشرط تُقترح من القيم الفعلية في الفهرس + القوائم المسماة،
  والتواريخ لها قوائم `now-7d` جاهزة.

## تغطية كتالوج السيناريوهات (ملف AML TMS)

القوالب محمّلة مسبقاً (معطلة، للمعايرة ثم التفعيل) — كلها SELECT خالص،
ولكل واحدة اختبار يثبت أنها تكتشف نمطها المزروع في بيانات التجربة:

| ID | السيناريو | جوهر الاستعلام |
|---|---|---|
| TM-01 | Cash Structuring | `cnt >= 3 AND total >= 9000` مع WHERE بين 9000-9999 |
| TM-02 | Pass-Through | `outflow >= 0.8 * inflow` عبر FILTER على is_debit |
| TM-03 | Round Amounts | حقل محسوب `is_round` ثم `round_cnt >= 5` |
| TM-04 | High-Risk Jurisdictions | `merchant_country IN (@high_risk_countries)` |
| TM-05 | Spike vs Profile | `total > 3 * declared` حيث declared = MAX(حقل العميل المصرح) |
| TM-06 | Large Single vs History | `biggest >= 10000 AND biggest >= 5 * avg_size` |
| TM-07 | Dormant Reactivation | `recent >= 1 AND prior == 0` بفلاتر now-7d |
| TM-08 | Layering (سلاسل) | ❌ ليست SELECT — تتطلب Graph Analysis (وحدة مستقبلية) |
| TM-09 | Many Counterparties | `COUNT(DISTINCT counterparty_wallet_id) >= 10` |
| TM-10 | New Corridor | `GROUP BY wallet_id, merchant_country` + `prior_cnt == 0` |
| TM-11 | Gambling Share | `gambling >= 0.7 * total` عبر `@gambling_mccs` |
| TM-12 | PEP/List Exposure | شق القوائم مغطى؛ الفحص الاسمي الضبابي وحدة مستقبلية |

## التنفيذ

- **معاينة**: كل قاعدة تولّد SQL مقروءاً + جسم استعلام Elasticsearch كاملاً
  (composite aggregation للمفاتيح المركبة، filter aggs للتجميعات المسماة،
  bucket_selector بسكربت Painless مُترجَم من تعبير HAVING، runtime fields
  للحقول المحسوبة).
- **المحرك**: يدفع النافذة الزمنية إلى ES ثم يقيّم WHERE/التجميعات/HAVING
  بدلالات مطابقة تماماً للـ DSL — فتعمل النتائج نفسها على المحاكي والكلاستر الحقيقي.
- **التنبيهات**: تسجّل الكيان (المفتاح المركب)، قيم كل التجميعات، وتعبير HAVING
  الذي تحقق — قابلية تفسير كاملة، مع سير تحقيق
  (New → In Review → Escalated → Closed) ومنع تكرار للتنبيهات المفتوحة
  وversioning للقواعد وجدولة حسب تردد كل قاعدة.

## بنية المشروع

```
dyaf/
├── expr.py        # لغة التعبيرات: HAVING والحقول المحسوبة (parser/eval/Painless)
├── conditions.py  # أشجار WHERE: تقييم + ترجمة ES + date math + @lists + SQL
├── rules.py       # نموذج القاعدة SELECT + التحقق + توليد SQL
├── dsl.py         # ترجمة القاعدة إلى ES DSL كاملاً
├── engine.py      # المنفّذ + المجدول + التنبيهات
├── es_client.py   # عميل Elasticsearch (اتصال/مصادقة/فهارس/بحث)
├── fake_es.py     # محاكي ES للتطوير والاختبار
├── ingest.py      # القيد المزدوج + JOINs + CSV + mappings + بيانات التجربة
├── templates.py   # كتالوج قوالب TM-01..TM-12
├── store.py       # SQLite التشغيلي (قواعد/تنبيهات/قوائم/إعدادات/محافظ)
├── api.py         # FastAPI + نقاط النهاية كاملة
└── ui/index.html  # منشئ القواعد المرئي بشكل SELECT
```
