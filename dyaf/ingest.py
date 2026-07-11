"""Ingest layer: double-entry transaction model, configurable JOINs,
CSV import/templates, index mappings and demo seeding.

Storage model (as designed with the business):
every logical transaction produces TWO documents in the `transactions`
index — one for the sender side and one for the receiver side:

    { transaction_id, reference_number, amount, fee, currency,
      executed_at, transaction_type_id (+ names), merchant_*,
      is_debit: true|false,          # true = this row is the sender side
      wallet_id: <this side's wallet>,
      counterparty_wallet_id: <the other side>,
      wallet_*: full wallet attributes JOINed from the wallets master,
      counterparty_*: joined counterparty attributes }

Elasticsearch has no query-time joins, so JOINs are applied at indexing
time from configurable join definitions:
    {"source_field": "wallet_id", "target": "wallets"|"transaction_types"|
     "internal_wallets", "prefix": "wallet_"}
"""
from __future__ import annotations

import csv
import io
from typing import Optional

from .store import Store

HIGH_RISK_COUNTRIES = ["IR", "KP", "SY", "MM"]

TRANSACTION_CSV_COLUMNS = [
    "transaction_id", "sender_wallet_id", "receiver_wallet_id", "amount",
    "executed_at", "transaction_type_id", "reference_number", "fee",
    "currency", "merchant_id", "merchant_name", "merchant_category",
    "merchant_country",
]
REQUIRED_TX = ("transaction_id", "sender_wallet_id", "receiver_wallet_id",
               "amount", "executed_at", "transaction_type_id")

WALLET_CSV_COLUMNS = [
    "wallet_id", "owner_name", "nationality", "residence_country",
    "date_of_birth", "risk_rating", "kyc_status", "pep_status",
    "wallet_type", "created_at",
]
REQUIRED_WALLET = ("wallet_id", "owner_name")

DEFAULT_TX_JOINS = [
    {"source_field": "wallet_id", "target": "wallets", "prefix": "wallet_"},
    {"source_field": "counterparty_wallet_id", "target": "wallets",
     "prefix": "counterparty_", "fields": ["owner_name", "wallet_type",
                                           "residence_country", "risk_rating"]},
    {"source_field": "wallet_id", "target": "internal_wallets", "prefix": "wallet_"},
    {"source_field": "counterparty_wallet_id", "target": "internal_wallets",
     "prefix": "counterparty_"},
    {"source_field": "transaction_type_id", "target": "transaction_types",
     "prefix": ""},
]

BUILTIN_DATASOURCES = [
    {"name": "transactions", "es_index": "transactions",
     "timestamp_field": "executed_at", "id_field": "doc_id",
     "required_fields": list(REQUIRED_TX), "joins": DEFAULT_TX_JOINS,
     "doubleentry": True, "builtin": True},
    {"name": "wallets", "es_index": "wallets",
     "timestamp_field": None, "id_field": "wallet_id",
     "required_fields": list(REQUIRED_WALLET), "joins": [], "builtin": True},
]


# ----------------------------------------------------------------------
# JOIN engine (indexing-time denormalization)
# ----------------------------------------------------------------------

def _join_lookup(store: Store, target: str) -> tuple[dict, Optional[dict]]:
    """Returns (key -> record, column_rename or None=all columns)."""
    if target == "wallets":
        return {w["wallet_id"]: w for w in store.list_wallets()}, None
    if target == "transaction_types":
        return ({t["type_id"]: t for t in store.list_transaction_types()},
                {"name_en": "transaction_type_en", "name_ar": "transaction_type_ar"})
    if target == "internal_wallets":
        return ({iw["wallet_id"]: iw for iw in store.list_internal_wallets()},
                {"name": "internal_wallet_name"})
    raise ValueError(f"Unknown join target '{target}'")


JOIN_TARGETS = ("wallets", "transaction_types", "internal_wallets")


def apply_joins(store: Store, rows: list[dict], joins: list[dict]) -> list[dict]:
    for join in joins or []:
        table, rename = _join_lookup(store, join["target"])
        prefix = join.get("prefix", "")
        only = set(join.get("fields") or [])
        key_field = join["source_field"]
        for row in rows:
            match = table.get(row.get(key_field)) or {}
            if rename is None:
                for col, val in match.items():
                    if col != "wallet_id" and (not only or col in only):
                        row[f"{prefix}{col}"] = val
            else:
                for col, out in rename.items():
                    if not only or col in only:
                        row[f"{prefix}{out}"] = match.get(col)
    return rows


# ----------------------------------------------------------------------
# Double-entry explode: 1 logical transaction -> 2 side documents
# ----------------------------------------------------------------------

def explode_double_entry(tx_rows: list[dict]) -> list[dict]:
    docs = []
    for tx in tx_rows:
        for is_debit, side, other in ((True, "sender_wallet_id", "receiver_wallet_id"),
                                      (False, "receiver_wallet_id", "sender_wallet_id")):
            if not tx.get(side):
                continue
            doc = {k: v for k, v in tx.items()
                   if k not in ("sender_wallet_id", "receiver_wallet_id")}
            doc["wallet_id"] = tx[side]
            doc["counterparty_wallet_id"] = tx.get(other)
            doc["is_debit"] = is_debit
            doc["doc_id"] = f"{tx.get('transaction_id')}-{'D' if is_debit else 'C'}"
            docs.append(doc)
    return docs


# ----------------------------------------------------------------------
# Index mappings
# ----------------------------------------------------------------------

_DYNAMIC_TEMPLATES = [{"strings_as_keywords": {
    "match_mapping_type": "string", "mapping": {"type": "keyword"}}}]


def wallets_mappings() -> dict:
    return {"dynamic": True, "dynamic_templates": _DYNAMIC_TEMPLATES,
            "properties": {
                "wallet_id": {"type": "keyword"},
                "owner_name": {"type": "keyword"},
                "date_of_birth": {"type": "date", "ignore_malformed": True},
                "pep_status": {"type": "boolean"},
                "created_at": {"type": "date", "ignore_malformed": True},
            }}


def transactions_mappings() -> dict:
    return {"dynamic": True, "dynamic_templates": _DYNAMIC_TEMPLATES,
            "properties": {
                "doc_id": {"type": "keyword"},
                "transaction_id": {"type": "keyword"},
                "wallet_id": {"type": "keyword"},
                "counterparty_wallet_id": {"type": "keyword"},
                "is_debit": {"type": "boolean"},
                "amount": {"type": "double"},
                "fee": {"type": "double"},
                "executed_at": {"type": "date"},
                "transaction_type_id": {"type": "long"},
                "wallet_pep_status": {"type": "boolean"},
                "wallet_date_of_birth": {"type": "date", "ignore_malformed": True},
                "wallet_created_at": {"type": "date", "ignore_malformed": True},
            }}


def dynamic_mappings(timestamp_field: Optional[str]) -> dict:
    props = {timestamp_field: {"type": "date"}} if timestamp_field else {}
    return {"dynamic": True, "dynamic_templates": _DYNAMIC_TEMPLATES,
            "properties": props}


def mappings_for(config: dict) -> dict:
    if config["name"] == "transactions":
        return transactions_mappings()
    if config["name"] == "wallets":
        return wallets_mappings()
    return dynamic_mappings(config.get("timestamp_field"))


# ----------------------------------------------------------------------
# CSV parsing / templates
# ----------------------------------------------------------------------

def _coerce(value: Optional[str]):
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v) if v.lstrip("+-").isdigit() else float(v)
    except ValueError:
        return v


def parse_csv(content: str, required: tuple) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(content))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        return [], [f"Missing required column(s): {', '.join(missing)}"]
    rows = []
    for i, raw in enumerate(reader, start=2):
        row = {k: _coerce(v) for k, v in raw.items() if k}
        empty = [c for c in required if row.get(c) in (None, "")]
        if empty:
            errors.append(f"line {i}: missing value(s) for {', '.join(empty)}")
            continue
        rows.append(row)
    return rows, errors


def csv_template(config: dict, live_fields: Optional[list] = None) -> str:
    name = config["name"]
    if name == "transactions":
        cols, sample = TRANSACTION_CSV_COLUMNS, {
            "transaction_id": "TX-100001", "sender_wallet_id": "W-1001",
            "receiver_wallet_id": "W-2001", "amount": "9500.00",
            "executed_at": "2026-07-10T14:30:00", "transaction_type_id": "3",
            "reference_number": "REF-00100001", "fee": "47.50", "currency": "USD"}
    elif name == "wallets":
        cols, sample = WALLET_CSV_COLUMNS, {
            "wallet_id": "W-1001", "owner_name": "Ahmad Khalil",
            "nationality": "JO", "residence_country": "JO",
            "date_of_birth": "1988-04-12", "risk_rating": "Medium",
            "kyc_status": "Verified", "pep_status": "false",
            "wallet_type": "Customer Wallet", "created_at": "2024-03-15T09:30:00"}
    else:
        cols = list(config.get("required_fields") or [])
        if config.get("id_field") and config["id_field"] not in cols:
            cols.insert(0, config["id_field"])
        if config.get("timestamp_field") and config["timestamp_field"] not in cols:
            cols.append(config["timestamp_field"])
        for f in live_fields or []:
            if f["name"] not in cols and f["name"] != "doc_id":
                cols.append(f["name"])
        if not cols:
            cols = ["id", "value"]
        sample = {}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    writer.writerow({c: sample.get(c, "") for c in cols})
    return buf.getvalue()


# ----------------------------------------------------------------------
# Demo seed — patterns aligned with the AML TMS scenario catalog (TM-xx)
# ----------------------------------------------------------------------

TRANSACTION_TYPES = [
    (1, "P2P Transfer", "تحويل بين محفظتين"),
    (2, "Cash In", "إيداع نقدي"),
    (3, "Cash Out", "سحب نقدي"),
    (4, "Card Payment", "دفع بالبطاقة"),
    (5, "International Remittance", "حوالة دولية"),
    (6, "Bill Payment", "دفع فواتير"),
]

WALLETS = [
    {"wallet_id": "W-1001", "owner_name": "Ahmad Khalil", "nationality": "JO",
     "residence_country": "JO", "date_of_birth": "1988-04-12",
     "risk_rating": "Medium", "kyc_status": "Verified", "pep_status": False,
     "wallet_type": "Customer Wallet", "created_at": "2024-03-15T09:30:00",
     "expected_weekly_volume": 2000},
    {"wallet_id": "W-1002", "owner_name": "Layla Hassan", "nationality": "JO",
     "residence_country": "JO", "date_of_birth": "1992-09-03",
     "risk_rating": "Low", "kyc_status": "Verified", "pep_status": False,
     "wallet_type": "Customer Wallet", "created_at": "2023-11-02T14:10:00",
     "expected_weekly_volume": 100000},
    {"wallet_id": "W-1003", "owner_name": "Omar Nasser", "nationality": "SY",
     "residence_country": "SY", "date_of_birth": "1979-01-25",
     "risk_rating": "High", "kyc_status": "Pending", "pep_status": True,
     "wallet_type": "Customer Wallet", "created_at": "2026-06-20T11:45:00",
     "expected_weekly_volume": 1000},
    {"wallet_id": "W-1004", "owner_name": "Sara Aziz", "nationality": "JO",
     "residence_country": "AE", "date_of_birth": "1995-06-30",
     "risk_rating": "Low", "kyc_status": "Verified", "pep_status": False,
     "wallet_type": "Customer Wallet", "created_at": "2026-07-01T08:00:00",
     "expected_weekly_volume": 3000},
    {"wallet_id": "W-1005", "owner_name": "Khaled Odeh", "nationality": "JO",
     "residence_country": "JO", "date_of_birth": "1985-11-08",
     "risk_rating": "Low", "kyc_status": "Verified", "pep_status": False,
     "wallet_type": "Customer Wallet", "created_at": "2022-05-19T16:20:00",
     "expected_weekly_volume": 50000},
    {"wallet_id": "W-1006", "owner_name": "Dormant Dana", "nationality": "JO",
     "residence_country": "JO", "date_of_birth": "1990-02-14",
     "risk_rating": "Low", "kyc_status": "Verified", "pep_status": False,
     "wallet_type": "Customer Wallet", "created_at": "2024-01-05T10:00:00",
     "expected_weekly_volume": 500},
    {"wallet_id": "W-2001", "owner_name": "Agent - Downtown Branch",
     "wallet_type": "Agent Wallet", "risk_rating": "Low",
     "kyc_status": "Verified", "pep_status": False,
     "created_at": "2021-01-10T09:00:00"},
    {"wallet_id": "W-2002", "owner_name": "Agent - Airport Kiosk",
     "wallet_type": "Agent Wallet", "risk_rating": "Low",
     "kyc_status": "Verified", "pep_status": False,
     "created_at": "2021-01-10T09:00:00"},
    {"wallet_id": "W-9001", "owner_name": "Card Settlement Wallet",
     "wallet_type": "Internal Wallet", "risk_rating": "Low",
     "kyc_status": "Verified", "pep_status": False,
     "created_at": "2020-06-01T00:00:00"},
    {"wallet_id": "W-9002", "owner_name": "Remittance Settlement Wallet",
     "wallet_type": "Internal Wallet", "risk_rating": "Low",
     "kyc_status": "Verified", "pep_status": False,
     "created_at": "2020-06-01T00:00:00"},
]

INTERNAL_WALLETS = [
    ("W-9001", "Card Settlement", "Settlement wallet for card scheme transactions"),
    ("W-9002", "Remittance Settlement", "Settlement wallet for remittance partners"),
]

DEFAULT_LISTS = [
    ("high_risk_countries", HIGH_RISK_COUNTRIES,
     "FATF high-risk and monitored jurisdictions"),
    ("gambling_mccs", ["Gambling", "Casino", "Betting"],
     "Gambling merchant categories"),
]


def seed_reference_data(store: Store) -> None:
    for w in WALLETS:
        store.upsert_wallet(w)
    for t in TRANSACTION_TYPES:
        store.upsert_transaction_type(*t)
    for iw in INTERNAL_WALLETS:
        store.upsert_internal_wallet(*iw)
    for name, values, desc in DEFAULT_LISTS:
        if store.get_list(name) is None:
            store.upsert_list(name, values, desc)


def seed_transactions(rng_seed: int = 42) -> list[dict]:
    """Logical transactions containing one deliberate pattern per TM scenario."""
    import random
    from datetime import datetime, timedelta, timezone
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc)
    seq = 0
    txs: list[dict] = []

    def tx(sender, receiver, amount, hours_ago, type_id, **kw):
        nonlocal seq
        seq += 1
        return {"transaction_id": f"TX-{seq:06d}",
                "sender_wallet_id": sender, "receiver_wallet_id": receiver,
                "amount": round(amount, 2),
                "executed_at": (now - timedelta(hours=hours_ago)).isoformat(),
                "transaction_type_id": type_id,
                "reference_number": f"REF-{seq:08d}",
                "fee": round(amount * 0.005, 2), "currency": "USD", **kw}

    customers = ["W-1001", "W-1002", "W-1004", "W-1005"]
    # normal background over the last 30 days
    for _ in range(150):
        s = rng.choice(customers)
        r = rng.choice([w for w in customers + ["W-2001", "W-2002"] if w != s])
        txs.append(tx(s, r, rng.uniform(10, 800), rng.uniform(1, 720),
                      rng.choice([1, 2, 3, 6])))

    # TM-01: structuring — 12 cash-outs 9,000-9,900 in 24h (W-1001)
    for _ in range(12):
        txs.append(tx("W-1001", "W-2001", rng.uniform(9000, 9900),
                      rng.uniform(0.5, 23), 3))
    # TM-02: pass-through — W-1004 receives 30k then forwards 28k within 2 days
    txs.append(tx("W-1005", "W-1004", 30000, 47, 1))
    for _ in range(4):
        txs.append(tx("W-1004", "W-2002", 7000, rng.uniform(2, 40), 1))
    # TM-03: repeated round amounts (W-1002: 6 x exactly 5,000)
    for _ in range(6):
        txs.append(tx("W-1002", "W-1005", 5000, rng.uniform(1, 100), 1))
    # TM-04 / TM-12: PEP remittances to high-risk countries (W-1003)
    # recent burst within the week + older history (keeps TM-07 specific)
    for _ in range(5):
        txs.append(tx("W-1003", "W-9002", rng.uniform(1500, 4000),
                      rng.uniform(1, 100), 5,
                      merchant_country=rng.choice(HIGH_RISK_COUNTRIES)))
    for _ in range(4):
        txs.append(tx("W-1003", "W-9002", rng.uniform(500, 1500),
                      rng.uniform(200, 600), 5,
                      merchant_country=rng.choice(HIGH_RISK_COUNTRIES)))
    # TM-06: single large transfer (W-1002 -> W-1005)
    txs.append(tx("W-1002", "W-1005", 75000, 30, 1))
    # TM-07: dormant reactivation — W-1006 only ever transacts this week
    txs.append(tx("W-1006", "W-2001", 950, 18, 3))
    txs.append(tx("W-1006", "W-2001", 990, 40, 3))
    # TM-09: many distinct counterparties (W-1005 pays 12 different wallets)
    for i in range(12):
        txs.append(tx("W-1005", f"W-30{i:02d}", rng.uniform(50, 200),
                      rng.uniform(1, 144), 1))
    # TM-11-ish: gambling card spend dominates W-1004's card usage
    for i in range(9):
        txs.append(tx("W-1004", "W-9001", rng.uniform(200, 900),
                      rng.uniform(1, 300), 4,
                      merchant_id=f"M-{7000 + i}",
                      merchant_name=f"Lucky Star Casino {i}",
                      merchant_category="Gambling", merchant_country="MT"))
    for i in range(3):
        txs.append(tx("W-1004", "W-9001", rng.uniform(20, 90),
                      rng.uniform(1, 300), 4,
                      merchant_id=f"M-{8000 + i}",
                      merchant_name="City Supermarket",
                      merchant_category="Grocery", merchant_country="AE"))
    return txs
