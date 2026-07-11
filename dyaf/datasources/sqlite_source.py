"""SQLite-backed datasources (reference implementation).

Field lists are discovered from the SQLite schema at call time (PRAGMA
table_info) — nothing is hardcoded, mirroring how the Elasticsearch adapter
reads the index mapping. The transactions source is denormalized with the
sender/receiver wallet attributes (prefixed ``sender_`` / ``receiver_``),
the transaction type names, and the internal-wallet classification, the same
shape an enriched Elasticsearch index would have.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..core.database import Database
from ..rules import conditions
from .base import DataSource, FieldInfo

_SQLITE_TYPE_MAP = {
    "TEXT": "keyword",
    "REAL": "double",
    "INTEGER": "long",
}


def _column_fields(db: Database, table: str, exclude: tuple = ()) -> list[FieldInfo]:
    cols = db.query(f"PRAGMA table_info({table})")
    out = []
    for c in cols:
        if c["name"] in exclude:
            continue
        ftype = _SQLITE_TYPE_MAP.get(str(c["type"]).upper(), "keyword")
        out.append(FieldInfo(name=c["name"], type=ftype))
    return out


class TransactionsDataSource(DataSource):
    name = "transactions"
    timestamp_field = "executed_at"

    def __init__(self, db: Database):
        self.db = db

    def get_fields(self) -> list[FieldInfo]:
        fields = []
        for f in _column_fields(self.db, "transactions", exclude=("extra",)):
            ftype = "date" if f.name == self.timestamp_field else f.type
            fields.append(FieldInfo(f.name, ftype))
        # Denormalized wallet attributes for both legs of the transaction
        for prefix in ("sender", "receiver"):
            for f in _column_fields(self.db, "wallets", exclude=("wallet_id",)):
                ftype = "boolean" if f.name == "pep_status" else f.type
                fields.append(FieldInfo(f"{prefix}_{f.name}", ftype))
        # Transaction type names + internal wallet classification
        fields.append(FieldInfo("transaction_type_en", "keyword"))
        fields.append(FieldInfo("transaction_type_ar", "keyword"))
        fields.append(FieldInfo("sender_internal_wallet_name", "keyword"))
        fields.append(FieldInfo("receiver_internal_wallet_name", "keyword"))
        # Dynamic extra attributes present in stored transactions
        seen = {f.name for f in fields}
        for row in self.db.query("SELECT extra FROM transactions WHERE extra != '{}' LIMIT 500"):
            import json
            for k, v in (json.loads(row["extra"] or "{}")).items():
                if k not in seen:
                    seen.add(k)
                    ftype = "double" if isinstance(v, (int, float)) else "keyword"
                    fields.append(FieldInfo(k, ftype))
        return fields

    def _enrich(self, rows: list[dict]) -> list[dict]:
        wallets = {w["wallet_id"]: w for w in self.db.list_wallets()}
        types = {t["type_id"]: t for t in self.db.list_transaction_types()}
        internal = {iw["wallet_id"]: iw for iw in self.db.list_internal_wallets()}
        for r in rows:
            for prefix, key in (("sender", "sender_wallet_id"), ("receiver", "receiver_wallet_id")):
                w = wallets.get(r.get(key)) or {}
                for wk, wv in w.items():
                    if wk != "wallet_id":
                        r[f"{prefix}_{wk}"] = wv
                iw = internal.get(r.get(key))
                r[f"{prefix}_internal_wallet_name"] = iw["name"] if iw else None
            t = types.get(r.get("transaction_type_id")) or {}
            r["transaction_type_en"] = t.get("name_en")
            r["transaction_type_ar"] = t.get("name_ar")
        return rows

    def fetch(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
              condition: Optional[dict] = None) -> list[dict]:
        rows = self.db.list_transactions(
            since=start.isoformat() if start else None,
            until=end.isoformat() if end else None,
        )
        rows = self._enrich(rows)
        if condition:
            rows = [r for r in rows if conditions.evaluate(condition, r)]
        return rows


class WalletsDataSource(DataSource):
    name = "wallets"
    timestamp_field = None  # reference data, no event time

    def __init__(self, db: Database):
        self.db = db

    def get_fields(self) -> list[FieldInfo]:
        fields = []
        for f in _column_fields(self.db, "wallets"):
            ftype = "boolean" if f.name == "pep_status" else f.type
            fields.append(FieldInfo(f.name, ftype))
        return fields

    def fetch(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
              condition: Optional[dict] = None) -> list[dict]:
        rows = self.db.list_wallets()
        if condition:
            rows = [r for r in rows if conditions.evaluate(condition, r)]
        return rows
