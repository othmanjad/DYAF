# DYAF — AML & Fraud Detection Platform

منصة كشف غسيل الأموال والاحتيال مبنية بلغة Python فوق نموذج بيانات المنصة المالية
(المحافظ الإلكترونية، الحركات المالية، أنواع العمليات، والمحافظ الداخلية)،
مع محرك قواعد قابل للتوسّع وواجهة **Rule Builder** مرئية لبناء القواعد بدون كتابة كود.

A rule-based AML & Fraud detection platform built in Python on top of the
financial platform's data model (Wallets, Transactions, Transaction Types,
Internal Wallets), with an extensible rule engine and a visual Rule Builder.

---

## التشغيل السريع / Quick Start

```bash
pip install -r requirements.txt

# 1) الاختبارات
python -m pytest tests/ -q

# 2) تجربة شاملة من الطرف إلى الطرف (بيانات تجريبية + قواعد + تنبيهات)
python demo.py

# 3) تشغيل الخادم وواجهة Rule Builder
python -m dyaf.api.app          # ثم افتح http://127.0.0.1:8000
```

---

## البنية / Architecture

```
dyaf/
├── core/                  # نموذج بيانات المنصة المالية
│   ├── models.py          #   Wallets, Transactions, TransactionTypes, InternalWalletConfig
│   └── database.py        #   SQLite schema + persistence
├── datasources/           # طبقة مصادر البيانات (اكتشاف الحقول ديناميكياً)
│   ├── base.py            #   DataSource interface + registry
│   ├── sqlite_source.py   #   المصدر المرجعي (denormalized مثل فهرس Elasticsearch)
│   └── elasticsearch_source.py  # محوّل Elasticsearch (يقرأ الحقول من الـ mapping)
├── rules/                 # محرك القواعد
│   ├── conditions.py      #   شجرة شروط متداخلة AND/OR/NOT + مشغّلات قابلة للتوسعة
│   ├── aggregations.py    #   سجل التجميعات: count, sum, avg, min, max,
│   │                      #   distinct_count, percentage, ratio, stddev*, moving_average*
│   ├── models.py          #   تعريف القاعدة (§6) + التحقق
│   ├── query_builder.py   #   توليد Elasticsearch DSL (لـ Query Preview)
│   ├── engine.py          #   التنفيذ: fetch → group by → aggregate → threshold → alerts
│   └── repository.py      #   تخزين القواعد مع versioning
├── alerts/                # التنبيهات (§7) + سير عمل التحقيق
├── scheduler.py           # المجدول (execution frequency لكل قاعدة)
├── api/                   # REST API (FastAPI) + واجهة Rule Builder
└── seed.py                # بيانات تجريبية بأنماط مشبوهة مقصودة
```

`*` مسجّلة كـ experimental (دعم مستقبلي حسب المتطلبات).

## تغطية المتطلبات / Requirements Coverage

### نموذج المنصة المالية
- **Transactions**: معرف الحركة، محفظتا المرسل/المستقبل، المبلغ، وقت التنفيذ، نوع العملية،
  الرقم المرجعي، الرسوم، العملة + حقول التاجر (المعرف، الاسم، التصنيف، الدولة) + حقول إضافية ديناميكية.
- **Wallets**: المعرف، اسم المالك، الجنسية، دولة الإقامة، تاريخ الميلاد، درجة المخاطر،
  KYC، PEP، نوع المحفظة (Customer / Agent / Internal).
- **Transaction Types**: جدول مرجعي بالاسمين العربي والإنجليزي.
- **Internal Wallets**: شاشة إعدادات (API + UI) لربط معرف المحفظة الداخلية باسم ووصف
  يوضّحان غرضها (تسوية البطاقات، تسوية الحوالات، ...).

### §6 Rule Execution — كل قاعدة تعرّف:
Data Source · Target Entity · Execution Frequency · Time Window · Aggregation Type ·
Output Threshold · Risk Score · Alert Severity

### §7 Alert Generation — كل تنبيه يحتوي:
Alert ID · Rule Name · Customer · Wallet · Transaction(s) · Risk Score · Alert Severity ·
Detection Time · Rule Version · Rule Result · Investigation Status
(سير عمل التحقيق: New → In Review → Escalated → Closed)

### §8 Rule Builder UI
واجهة ويب على `/` تدعم: شروط بالسحب والإفلات وإعادة الترتيب، مجموعات شروط متداخلة
(AND/OR/NOT)، التجميعات، Group By، فلاتر زمنية، اختيار الحقول ديناميكياً،
**Query Preview** (يعرض Elasticsearch DSL)، **Validate**، و **Test Rule** قبل الحفظ (dry-run بدون حفظ تنبيهات).

### §9 Supported Data Fields
لا توجد أسماء حقول ثابتة في المحرك: كل مصدر بيانات يكتشف حقوله وقت الطلب —
محوّل Elasticsearch يقرأ الـ index mapping، والمصدر المرجعي يقرأ مخطط الجداول
والحقول الإضافية المخزنة، فأي حقل جديد يصبح متاحاً تلقائياً في الـ Rule Builder.

### §10 Extensibility
نقاط توسعة معتمدة على السجلات (registries) بدون تعديل المحرك:
- `conditions.register_operator(...)` — مشغّلات مقارنة جديدة.
- `aggregations.register(...)` — تجميعات جديدة (ML scores, behavioral metrics, ...).
- `DataSourceRegistry.register(...)` — مصادر بيانات جديدة (Kafka/Streaming, Graph, ...).
- التنبيهات والقواعد كيانات مستقلة → يمكن ربط Case Management / Workflow Engine فوقها.

## REST API

| Method | Path | الوصف |
|---|---|---|
| GET | `/api/metadata` | مصادر البيانات، المشغّلات، التجميعات، الكيانات، درجات الخطورة |
| GET | `/api/datasources/{name}/fields` | اكتشاف الحقول ديناميكياً |
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

`python demo.py` يزرع بيانات فيها 4 أنماط مشبوهة ويعرّف 4 قواعد تكتشفها:

| القاعدة | النمط المزروع | النتيجة |
|---|---|---|
| Structuring Detection | 12 سحباً نقدياً بين 9,000–9,900 خلال 24 ساعة (W-1001) | تنبيه Critical |
| Large Single Transfer | تحويل واحد بقيمة 75,000 (W-1002) | تنبيه High |
| PEP Remittances to High-Risk Countries | حوالات PEP لدول عالية المخاطر بمجموع ~23,000 (W-1003) | تنبيه Critical |
| Gambling Spend Share | ~95% من إنفاق البطاقة لدى تجار قمار (W-1004) | تنبيه Medium |
