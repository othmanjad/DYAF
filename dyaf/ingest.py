"""Ingest layer: index bootstrap mappings, document enrichment, and CSV
import/export for the Elasticsearch detection indices.

Transactions are stored in Elasticsearch already *enriched* (denormalized
with sender/receiver wallet attributes, transaction type names and the
internal-wallet classification), the way a production ingest pipeline
would write them. Enrichment happens here at ingest time — seeding and
CSV upload share the same path.
"""
from __future__ import annotations

import csv
import io
from dataclasses import fields as dc_fields
from typing import Optional

from .core.database import Database
from .core.models import Transaction, Wallet

# ----------------------------------------------------------------------
# Base (uploadable) columns — derived from the platform dataclasses,
# not hand-maintained lists.
# ----------------------------------------------------------------------

_NUMERIC_TX = {"amount": float, "fee": float, "transaction_type_id": int}
_REQUIRED_TX = ("transaction_id", "sender_wallet_id", "receiver_wallet_id",
                "amount", "executed_at", "transaction_type_id")
_REQUIRED_WALLET = ("wallet_id", "owner_name")

_SAMPLE_ROWS = {
    "transactions": {
        "transaction_id": "TX-100001", "sender_wallet_id": "W-1001",
        "receiver_wallet_id": "W-2001", "amount": "9500.00",
        "executed_at": "2026-07-10T14:30:00", "transaction_type_id": "3",
        "reference_number": "REF-00100001", "fee": "47.50", "currency": "USD",
        "merchant_id": "", "merchant_name": "", "merchant_category": "",
        "merchant_country": "",
    },
    "wallets": {
        "wallet_id": "W-1001", "owner_name": "Ahmad Khalil", "nationality": "JO",
        "residence_country": "JO", "date_of_birth": "1988-04-12",
        "risk_rating": "Medium", "kyc_status": "Verified", "pep_status": "false",
        "wallet_type": "Customer Wallet", "created_at": "2024-03-15T09:30:00",
    },
}


def transaction_csv_columns() -> list[str]:
    return [f.name for f in dc_fields(Transaction) if f.name != "extra"]


def wallet_csv_columns() -> list[str]:
    return [f.name for f in dc_fields(Wallet)]


def csv_template(datasource: str) -> str:
    """CSV template (header + one sample row) for a detection datasource."""
    if datasource == "transactions":
        cols = transaction_csv_columns()
    elif datasource == "wallets":
        cols = wallet_csv_columns()
    else:
        raise ValueError(f"No CSV template for datasource '{datasource}'")
    sample = _SAMPLE_ROWS[datasource]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    writer.writerow({c: sample.get(c, "") for c in cols})
    return buf.getvalue()


# ----------------------------------------------------------------------
# Elasticsearch index mappings (bootstrap on first run)
# ----------------------------------------------------------------------

def _wallet_properties() -> dict:
    return {
        "wallet_id": {"type": "keyword"},
        "owner_name": {"type": "keyword"},
        "nationality": {"type": "keyword"},
        "residence_country": {"type": "keyword"},
        "date_of_birth": {"type": "date", "ignore_malformed": True},
        "risk_rating": {"type": "keyword"},
        "kyc_status": {"type": "keyword"},
        "pep_status": {"type": "boolean"},
        "wallet_type": {"type": "keyword"},
        "created_at": {"type": "date", "ignore_malformed": True},
    }


def transactions_mappings() -> dict:
    props = {
        "transaction_id": {"type": "keyword"},
        "sender_wallet_id": {"type": "keyword"},
        "receiver_wallet_id": {"type": "keyword"},
        "amount": {"type": "double"},
        "executed_at": {"type": "date"},
        "transaction_type_id": {"type": "long"},
        "reference_number": {"type": "keyword"},
        "fee": {"type": "double"},
        "currency": {"type": "keyword"},
        "merchant_id": {"type": "keyword"},
        "merchant_name": {"type": "keyword"},
        "merchant_category": {"type": "keyword"},
        "merchant_country": {"type": "keyword"},
        "transaction_type_en": {"type": "keyword"},
        "transaction_type_ar": {"type": "keyword"},
        "sender_internal_wallet_name": {"type": "keyword"},
        "receiver_internal_wallet_name": {"type": "keyword"},
    }
    for prefix in ("sender", "receiver"):
        for name, spec in _wallet_properties().items():
            if name != "wallet_id":
                props[f"{prefix}_{name}"] = dict(spec)
    return {
        "dynamic": True,
        # extra CSV columns become searchable keyword/double fields automatically
        "dynamic_templates": [
            {"strings_as_keywords": {
                "match_mapping_type": "string",
                "mapping": {"type": "keyword"}}},
        ],
        "properties": props,
    }


def wallets_mappings() -> dict:
    return {
        "dynamic": True,
        "dynamic_templates": [
            {"strings_as_keywords": {
                "match_mapping_type": "string",
                "mapping": {"type": "keyword"}}},
        ],
        "properties": _wallet_properties(),
    }


def wallet_transactions_mappings() -> dict:
    """Per-direction view of transactions: each transaction is indexed
    twice — once as a *debit* for the sending wallet and once as a
    *credit* for the receiving wallet — so entity-centric rules can
    compare money-out vs money-in for the same wallet (e.g. total debit
    amount > total credit amount)."""
    mappings = transactions_mappings()
    mappings["properties"].update({
        "wallet_id": {"type": "keyword"},
        "direction": {"type": "keyword"},          # debit | credit
        "counterparty_wallet_id": {"type": "keyword"},
        "doc_id": {"type": "keyword"},
    })
    return mappings


def explode_wallet_transactions(rows: list[dict]) -> list[dict]:
    """Expand enriched transaction docs into per-wallet direction docs."""
    docs: list[dict] = []
    for r in rows:
        for direction, wallet_key, cp_key, suffix in (
                ("debit", "sender_wallet_id", "receiver_wallet_id", "D"),
                ("credit", "receiver_wallet_id", "sender_wallet_id", "C")):
            if not r.get(wallet_key):
                continue
            d = dict(r)
            d["wallet_id"] = r[wallet_key]
            d["direction"] = direction
            d["counterparty_wallet_id"] = r.get(cp_key)
            d["doc_id"] = f"{r.get('transaction_id')}-{suffix}"
            docs.append(d)
    return docs


# ----------------------------------------------------------------------
# Configurable enrichment (shared by seeding + CSV upload)
#
# An enrichment is {"key_field", "lookup", "prefix"}: for each row, the
# value of key_field is looked up in a platform reference table and the
# lookup's output columns are attached with the prefix. Enrichment lists
# are stored per-datasource in datasource_configs, so admins can wire new
# joins from settings without code changes.
# ----------------------------------------------------------------------

def _wallets_lookup(db: Database) -> tuple[dict, dict]:
    rows = {w["wallet_id"]: w for w in db.list_wallets()}
    columns = None  # None => every column except the key, name kept as-is
    return rows, columns


def _transaction_types_lookup(db: Database) -> tuple[dict, dict]:
    rows = {t["type_id"]: t for t in db.list_transaction_types()}
    return rows, {"name_en": "transaction_type_en", "name_ar": "transaction_type_ar"}


def _internal_wallets_lookup(db: Database) -> tuple[dict, dict]:
    rows = {iw["wallet_id"]: iw for iw in db.list_internal_wallets()}
    return rows, {"name": "internal_wallet_name"}


LOOKUPS = {
    "wallets": {"fn": _wallets_lookup, "key": "wallet_id"},
    "transaction_types": {"fn": _transaction_types_lookup, "key": "type_id"},
    "internal_wallets": {"fn": _internal_wallets_lookup, "key": "wallet_id"},
}

# Reproduces the platform's canonical transaction enrichment
DEFAULT_TRANSACTION_ENRICHMENTS = [
    {"key_field": "sender_wallet_id", "lookup": "wallets", "prefix": "sender_"},
    {"key_field": "receiver_wallet_id", "lookup": "wallets", "prefix": "receiver_"},
    {"key_field": "sender_wallet_id", "lookup": "internal_wallets", "prefix": "sender_"},
    {"key_field": "receiver_wallet_id", "lookup": "internal_wallets", "prefix": "receiver_"},
    {"key_field": "transaction_type_id", "lookup": "transaction_types", "prefix": ""},
]


def apply_enrichments(db: Database, rows: list[dict], enrichments: list[dict]) -> list[dict]:
    """Attach prefixed lookup columns to each row per the enrichment config."""
    for e in enrichments or []:
        lookup = LOOKUPS.get(e.get("lookup"))
        if not lookup:
            continue
        table, columns = lookup["fn"](db)
        key_field, prefix = e.get("key_field"), e.get("prefix", "")
        key_name = lookup["key"]
        for r in rows:
            match = table.get(r.get(key_field)) or {}
            if columns is None:
                for col, val in match.items():
                    if col != key_name:
                        r[f"{prefix}{col}"] = val
            else:
                for col, out_name in columns.items():
                    r[f"{prefix}{out_name}"] = match.get(col)
    return rows


def enrich_transactions(db: Database, rows: list[dict]) -> list[dict]:
    """Canonical enrichment for the transactions datasource."""
    return apply_enrichments(db, rows, DEFAULT_TRANSACTION_ENRICHMENTS)


# ----------------------------------------------------------------------
# CSV parsing
# ----------------------------------------------------------------------

def _coerce(value: str):
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v) if v.lstrip("+-").isdigit() else float(v)
    except ValueError:
        return v


def parse_csv(content: str, required: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """Parse CSV text into typed row dicts; returns (rows, errors)."""
    errors: list[str] = []
    try:
        reader = csv.DictReader(io.StringIO(content))
        header = reader.fieldnames or []
    except csv.Error as e:
        return [], [f"Invalid CSV: {e}"]
    missing = [c for c in required if c not in header]
    if missing:
        return [], [f"Missing required column(s): {', '.join(missing)}"]

    rows: list[dict] = []
    for i, raw in enumerate(reader, start=2):  # header is line 1
        row = {k: _coerce(v) for k, v in raw.items() if k is not None and k != ""}
        empty = [c for c in required if row.get(c) in (None, "")]
        if empty:
            errors.append(f"line {i}: missing value(s) for {', '.join(empty)}")
            continue
        rows.append(row)
    return rows, errors


def parse_transactions_csv(content: str) -> tuple[list[dict], list[str]]:
    rows, errors = parse_csv(content, _REQUIRED_TX)
    valid = []
    for row in rows:
        try:
            for col, cast in _NUMERIC_TX.items():
                if row.get(col) is not None:
                    row[col] = cast(row[col])
            valid.append(row)
        except (TypeError, ValueError):
            errors.append(f"transaction {row.get('transaction_id')}: non-numeric "
                          f"value in {', '.join(_NUMERIC_TX)}")
    return valid, errors


def parse_wallets_csv(content: str) -> tuple[list[dict], list[str]]:
    rows, errors = parse_csv(content, _REQUIRED_WALLET)
    for row in rows:
        row["pep_status"] = bool(row.get("pep_status"))
    return rows, errors


# ----------------------------------------------------------------------
# Row -> platform model conversion (for the operational SQLite store)
# ----------------------------------------------------------------------

def transaction_model_from_row(row: dict) -> Transaction:
    base = set(transaction_csv_columns())
    kwargs = {k: row[k] for k in base if k in row and row[k] is not None}
    extra = {k: v for k, v in row.items() if k not in base and v is not None}
    kwargs.setdefault("reference_number", "")
    return Transaction(extra=extra, **kwargs)


def wallet_model_from_row(row: dict) -> Wallet:
    base = set(wallet_csv_columns())
    kwargs = {k: row[k] for k in base if k in row and row[k] is not None}
    return Wallet(**kwargs)
