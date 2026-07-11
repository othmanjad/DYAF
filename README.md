# DYAF — AML & Fraud Detection Platform

منصة كشف غسيل الأموال والاحتيال مبنية بلغة Python فوق نموذج بيانات المنصة المالية
(المحافظ الإلكترونية، الحركات المالية، أنواع العمليات، والمحافظ الداخلية)،
مع محرك قواعد قابل للتوسّع وواجهة **Rule Builder** مرئية لبناء القواعد بدون كتابة كود.

**طبقة الكشف تقرأ دائماً من Elasticsearch**: اكتشاف الحقول من الـ index mapping،
تنفيذ الاستعلامات عبر `_search`، إنشاء الفهارس تلقائياً أول مرة، واستيراد البيانات عبر ملفات CSV.

A rule-based AML & Fraud detection platform built in Python on top of the
financial platform's data model. The detection layer always reads from
Elasticsearch, with first-run index bootstrap and CSV import/export.

---

## التشغيل السريع / Quick Start

```bash
pip install -r requirements.txt

# 1) الاختبارات (تمر عبر مسار Elasticsearch الحقيقي ضد محاكي مطابق للـ API)
python -m pytest tests/ -q

# 2) تجربة شاملة من الطرف إلى الطرف
python demo.py

# 3) تشغيل الخادم وواجهة Rule Builder
export ELASTICSEARCH_URL=http://localhost:9200   # عنوان الـ cluster
python -m dyaf.api.app                            # ثم افتح http://127.0.0.1:8000
```

> **ملاحظة:** إذا لم يتم ضبط `ELASTICSEARCH_URL`، يشغّل النظام تلقائياً
> **محاكي Elasticsearch مدمج** (`dyaf/testing/fake_es.py`) يطبّق نفس REST API
> (index creation, mapping, bulk, search) حتى تعمل المنصة فوراً بدون بنية تحتية.
> للإنتاج اضبط المتغير على cluster حقيقي — الكود لا يتغير إطلاقاً.

## إعدادات الاتصال والمصادقة / Connection & Authentication

كل الإعدادات عبر متغيرات البيئة، أو ملف **`.env`** في مجلد التشغيل
(انسخ `.env.example` إلى `.env` وعدّل القيم — متغيرات البيئة الفعلية لها الأولوية):

| المتغير | الوصف |
|---|---|
| `ELASTICSEARCH_URL` | عنوان الـ cluster (مثال: `https://es.mycompany.com:9200`) |
| `ELASTICSEARCH_USERNAME` | اسم المستخدم (Basic Auth) |
| `ELASTICSEARCH_PASSWORD` | كلمة المرور |
| `ELASTICSEARCH_API_KEY` | بديل عن اسم المستخدم/كلمة المرور: API Key صادر من ES (له الأولوية عند ضبط الاثنين) |
| `ELASTICSEARCH_CA_CERT` | مسار شهادة CA للـ cluster (مثل `http_ca.crt` الذي يولّده ES 8) |
| `ELASTICSEARCH_VERIFY_CERTS` | `false` لتعطيل التحقق من الشهادات (للتطوير فقط) |
| `PORT` | منفذ خادم الويب (افتراضي 8000) |

مثال إعداد نموذجي لـ Elasticsearch 8 محلي:

```bash
cp .env.example .env
# ثم عدّل .env:
#   ELASTICSEARCH_URL=https://localhost:9200
#   ELASTICSEARCH_USERNAME=elastic
#   ELASTICSEARCH_PASSWORD=<كلمة المرور التي ولّدها ES عند التثبيت>
#   ELASTICSEARCH_CA_CERT=/etc/elasticsearch/certs/http_ca.crt
python -m dyaf.api.app
```

تبويب **Data** في الواجهة و `GET /api/es/health` يعرضان حالة المصادقة
(`auth_mode`: none / basic / api_key، و`authenticated`: نجاح/فشل الاعتماد) —
عند فشل الاعتماد تظهر رسالة واضحة بدل أخطاء غامضة. ملف `.env` مستثنى من git.

---

## Elasticsearch

| الميزة | التفاصيل |
|---|---|
| **إنشاء الفهرس أول مرة** | عند الإقلاع (أو زر *Create Missing Indices* في تبويب Data، أو `POST /api/es/setup`) يتم إنشاء فهرسي `transactions` و `wallets` بالـ mapping الكامل للمنصة إذا لم يكونا موجودين — العملية idempotent. |
| **فحص الحالة** | `GET /api/es/health` يعرض قابلية الوصول للـ cluster وعدد الوثائق في كل فهرس (معروض في تبويب Data). |
| **رفع CSV** | زر *Upload & Index* في تبويب Data (أو `POST /api/datasources/{name}/upload-csv`): يتحقق من الأعمدة المطلوبة، يحوّل الأنواع، يُثري حركات الـ transactions ببيانات المحافظ وأسماء أنواع العمليات، ثم يفهرسها bulk. أخطاء الصفوف تُرجَع تفصيلياً. |
| **تنزيل CSV Template** | زر *Download CSV Template* (أو `GET /api/datasources/{name}/csv-template`): ملف CSV جاهز بالأعمدة الصحيحة + صف مثال، لكل من transactions و wallets. |
| **أعمدة إضافية ديناميكية** | أي عمود إضافي في ملف الـ CSV (مثل `device_id`) يُفهرس تلقائياً (dynamic mapping) ويظهر فوراً كحقل متاح في الـ Rule Builder — متطلب §9. |
| **الإثراء عند الإدخال** | وثائق الحركات تُخزَّن في ES مُثراة (سمات محفظتي المرسل/المستقبل بـ `sender_*`/`receiver_*`، أسماء نوع العملية، تصنيف المحفظة الداخلية) كما يفعل ingest pipeline حقيقي. |

## البنية / Architecture

```
dyaf/
├── core/                  # نموذج بيانات المنصة المالية (المخزن التشغيلي)
│   ├── models.py          #   Wallets, Transactions, TransactionTypes, InternalWalletConfig
│   └── database.py        #   SQLite: القواعد، التنبيهات، الجداول المرجعية
├── datasources/           # طبقة مصادر البيانات
│   ├── base.py            #   DataSource interface + registry
│   └── elasticsearch_source.py  # المصدر الدائم: mapping introspection,
│                          #   ensure_index (bootstrap), bulk_index, _search pushdown
├── ingest.py              # mappings الفهارس، الإثراء، قوالب CSV، تحليل CSV
├── rules/                 # محرك القواعد
│   ├── conditions.py      #   شجرة شروط متداخلة AND/OR/NOT + مشغّلات قابلة للتوسعة
│   ├── aggregations.py    #   count, sum, avg, min, max, distinct_count,
│   │                      #   percentage, ratio, difference, stddev*, moving_average*
│   ├── models.py          #   تعريف القاعدة (§6) + التحقق
│   ├── query_builder.py   #   توليد Elasticsearch DSL (Query Preview + pushdown)
│   ├── engine.py          #   fetch من ES → group by → aggregate → threshold → alerts
│   └── repository.py      #   تخزين القواعد مع versioning
├── alerts/                # التنبيهات (§7) + سير عمل التحقيق
├── scheduler.py           # المجدول (execution frequency لكل قاعدة)
├── api/                   # REST API (FastAPI) + واجهة Rule Builder
├── testing/fake_es.py     # محاكي Elasticsearch (تطوير/اختبار فقط)
└── seed.py                # بيانات تجريبية بأنماط مشبوهة مقصودة
```

`*` مسجّلة كـ experimental (دعم مستقبلي حسب المتطلبات).

**تقسيم التخزين:** Elasticsearch هو مصدر القراءة الوحيد لمحرك القواعد (الحركات والمحافظ
المفهرسة). SQLite هو المخزن التشغيلي الصغير للمنصة نفسها: تعريفات القواعد وإصداراتها،
التنبيهات، أنواع العمليات، سجل المحافظ (المستخدم للإثراء)، وإعدادات المحافظ الداخلية.

## تغطية المتطلبات / Requirements Coverage

### §6 Rule Execution — كل قاعدة تعرّف:
Data Source · Target Entity · Execution Frequency · Time Window · Aggregation Type ·
Output Threshold · Risk Score · Alert Severity

**الفهارس الثلاثة:**
- `transactions` — الحركات المُثراة (سمات محفظتي المرسل/المستقبل بـ `sender_*`/`receiver_*`).
- `wallets` — المحافظ (تشمل `created_at` تاريخ إنشاء المحفظة).
- `wallet_transactions` — **عرض ثنائي الاتجاه**: كل حركة تُفهرس مرتين، مرة `debit` لمحفظة
  المرسل ومرة `credit` لمحفظة المستقبل (حقول `wallet_id`, `direction`,
  `counterparty_wallet_id`) — يتيح قواعد على مستوى العميل تقارن الصادر بالوارد،
  مثل «العميل الذي مجموع حركاته المدينة أكبر من الدائنة»:
  `difference(amount, numerator: direction=debit, denominator: direction=credit) > 0`.

**فلاتر التاريخ:** حقول النوع `date` تُقارن كتواريخ فعلية (وليس نصاً) في كل المشغّلات
(gt/gte/lt/lte/eq/between)، والواجهة تعرض منتقي تاريخ ووقت (datetime picker) تلقائياً
لهذه الحقول — يمكن مثلاً فلترة `executed_at >= 2026-07-01T00:00`.

**التواريخ النسبية (Date Math):** الشروط تقبل صيغ Elasticsearch النسبية
(`now`, `now-24h`, `now-7d`, `now-180d`...) وتُقيَّم وقت تنفيذ القاعدة — محلياً وفي
الـ cluster بنفس الدلالة، والواجهة توفرها كقائمة منسدلة جاهزة لحقول التاريخ.
هذا يفتح قواعد الـ baseline الزمنية، مثل كشف إعادة تنشيط الحساب الخامل (TM-07):
`compare(left: count(executed_at ≥ now-7d), right: count(executed_at < now-7d),
operation: left_when_right_zero) ≥ N` — أي «نشاط الآن ولا نشاط إطلاقاً قبل ذلك».

**الـ Threshold لكل عميل (Profile Deviation):** بدل الرقم الثابت، يمكن ربط الحد بحقل
خاص بكل كيان مع مضاعِف: `threshold: {operator: gt, value_field:
sender_expected_weekly_volume, multiplier: 3}` — «مجموع الحركات > 3 × النشاط
المُصرَّح للعميل» (TM-05/06). الصفات المُصرَّحة تُرفع كأعمدة إضافية في CSV المحافظ
(أعمدة مخصصة تُخزَّن وتُثرى تلقائياً)، والكيانات بلا قيمة أساس تُتخطى ولا تُنبَّه خطأً،
والتنبيه يسجّل الحد الفعلي المحسوب لذلك العميل.

**نمطا القواعد:**
- **Aggregation Rule**: تجميع (count/sum/avg/percentage/...) حسب Group By ومقارنة الناتج بالـ Threshold — مثل «5 سحوبات قرب الحد خلال 24 ساعة».
- **Match Rule**: بدون تجميع (اختر "none") — تنبيه مباشر لكل سجل مطابق للشروط، والـ Group By والـ Threshold اختياريان — مثل «أي حركة قيمتها أكبر من 1000 دينار». التنبيه يتضمن لقطة كاملة من الحركة المطابقة، ومع تحديد Group By يصبح التنبيه لكل كيان (محفظة مثلاً) بدل كل سجل.

### §7 Alert Generation — كل تنبيه يحتوي:
Alert ID · Rule Name · Customer · Wallet · Transaction(s) · Risk Score · Alert Severity ·
Detection Time · Rule Version · Rule Result · Investigation Status
(سير عمل التحقيق: New → In Review → Escalated → Closed)

### §8 Rule Builder UI
واجهة ويب على `/` تدعم: شروط بالسحب والإفلات وإعادة الترتيب، مجموعات شروط متداخلة
(AND/OR/NOT)، التجميعات، Group By، فلاتر زمنية، اختيار الحقول ديناميكياً،
**Query Preview** (Elasticsearch DSL)، **Validate**، و **Test Rule** قبل الحفظ (dry-run).

### §9 Supported Data Fields
لا توجد أسماء حقول ثابتة في المحرك: الحقول تُقرأ من الـ index mapping الحي
(`GET <index>/_mapping`)، وأي حقل جديد يُفهرس — يدوياً أو عبر عمود CSV إضافي —
يظهر تلقائياً في الـ Rule Builder ويصبح قابلاً للاستخدام في القواعد فوراً.

### §10 Extensibility
- **مصادر بيانات ديناميكية بدون كود**: من تبويب Data (أو `POST /api/datasource-configs`)
  يسجّل المسؤول أي فهرس Elasticsearch كمصدر قواعد جديد — الاسم، حقل الوقت، حقل المعرف،
  الأعمدة الإجبارية للـ CSV، وقواعد الإثراء — ويظهر فوراً في الـ Rule Builder وواجهة
  الاستيراد، مع إنشاء الفهرس تلقائياً وقالب CSV مشتق من إعداداته.
- **إثراء قابل للتهيئة**: قواعد الدمج تُعرَّف لكل مصدر كقائمة
  `{key_field, lookup, prefix}` ضد جداول المنصة المرجعية
  (wallets / transaction_types / internal_wallets) بدل أن تكون في الكود.
- **تجميع `compare` العام**: قارن أي تجميعين تبنيهما بحرية — نوع (count/sum/avg/min/max/
  distinct_count) + حقل + فلتر لكل طرف، والعملية طرح أو قسمة — مثل
  «عدد حركات debit − عدد حركات credit > 0» أو «متوسط صرف اليوم ÷ المتوسط العام > 5».
- **قيم الشروط كقوائم منسدلة**: حقل القيمة في الشروط يقترح القيم الفعلية الموجودة في
  الفهرس (terms aggregation عبر `/api/datasources/{ds}/fields/{field}/values`)
  بدل الكتابة اليدوية.
- `conditions.register_operator(...)` — مشغّلات مقارنة جديدة.
- `aggregations.register(...)` — تجميعات جديدة (ML scores, behavioral metrics, ...).
- التنبيهات والقواعد كيانات مستقلة → يمكن ربط Case Management / Workflow Engine فوقها.

## REST API

| Method | Path | الوصف |
|---|---|---|
| GET | `/api/es/health` | حالة الـ cluster وعدد الوثائق لكل فهرس |
| POST | `/api/es/setup?seed=` | إنشاء الفهارس الناقصة (bootstrap) + بذر بيانات تجريبية اختيارياً |
| GET | `/api/datasources/{name}/csv-template` | تنزيل قالب CSV (header + صف مثال) |
| POST | `/api/datasources/{name}/upload-csv` | رفع ملف CSV وفهرسته (multipart) |
| GET | `/api/datasources/{name}/fields` | اكتشاف الحقول ديناميكياً من الـ mapping |
| GET | `/api/datasources/{name}/fields/{field}/values` | القيم الفعلية لحقل (لقوائم الاقتراح) |
| GET/POST/DELETE | `/api/datasource-configs` | إدارة مصادر البيانات الديناميكية |
| GET | `/api/metadata` | مصادر البيانات، المشغّلات، التجميعات، الكيانات، الخطورات |
| POST | `/api/rules/validate` | التحقق من تعريف القاعدة |
| POST | `/api/rules/preview` | معاينة استعلام Elasticsearch DSL |
| POST | `/api/rules/test` | تجربة القاعدة (dry-run) قبل الحفظ |
| POST/GET | `/api/rules` | إنشاء / عرض القواعد (مع versioning) |
| PUT/DELETE | `/api/rules/{id}` | تعديل (يرفع الإصدار) / حذف |
| GET | `/api/rules/{id}/versions` | سجل إصدارات القاعدة |
| POST | `/api/rules/{id}/execute` | تنفيذ فوري |
| POST | `/api/scheduler/run` | تنفيذ كل القواعد المستحقة حسب ترددها |
| GET | `/api/alerts` | التنبيهات (فلترة بالقاعدة/الحالة) |
| PUT | `/api/alerts/{id}/status` | تحديث حالة التحقيق |
| GET/POST/DELETE | `/api/internal-wallets` | شاشة إعدادات المحافظ الداخلية |

## سيناريو التجربة / Demo Scenario

`python demo.py` — يجري السيناريو كاملاً عبر Elasticsearch:
إنشاء الفهارس أول مرة → بذر 155 حركة (bulk) → استيراد CSV بعمود جديد `device_id` →
اكتشاف الحقل الجديد تلقائياً → 5 قواعد (منها قاعدة مبنية على عمود الـ CSV الجديد) →
تشغيل المجدول → 5 تنبيهات:

| القاعدة | النمط المزروع | النتيجة |
|---|---|---|
| Structuring Detection | 12+ سحباً نقدياً بين 9,000–9,900 خلال 24 ساعة (W-1001) | Critical |
| Large Single Transfer | تحويل واحد بقيمة 75,000 (W-1002) | High |
| PEP Remittances to High-Risk Countries | حوالات PEP لدول عالية المخاطر (W-1003) | Critical |
| Gambling Spend Share | ~95% من إنفاق البطاقة لدى تجار قمار (W-1004) | Medium |
| Same Device Structuring | حركتان قرب الحد من نفس `device_id` (من ملف CSV) | High |

## ملاحظات إنتاجية

- التجميع حالياً يُنفَّذ في المحرك بعد جلب الصفوف المطابقة من ES (حد 10,000 صف
  لكل تنفيذ). الـ DSL الكامل للتجميعات جاهز في `query_builder.build_rule_query`
  — دفع التجميع بالكامل إلى ES هو الخطوة التالية للأحجام الكبيرة.
- وثائق ES مُثراة عند الإدخال؛ تغيير سمات محفظة لاحقاً يتطلب إعادة فهرسة
  الحركات القديمة إذا أردت انعكاسه عليها (سلوك ingest pipelines المعتاد).
