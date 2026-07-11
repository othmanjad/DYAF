"""SQLite persistence layer for the platform's core tables.

The schema mirrors the production financial platform (Transactions, Wallets,
Transaction Types, Internal Wallets) plus the AML engine's own tables
(rules, rule_versions, alerts). SQLite keeps the reference implementation
self-contained; the datasource abstraction (dyaf.datasources) is what the
rule engine talks to, so swapping in Elasticsearch requires no engine change.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, date
from typing import Iterable, Optional

from .models import Wallet, Transaction, TransactionType, InternalWalletConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    wallet_id           TEXT PRIMARY KEY,
    owner_name          TEXT NOT NULL,
    nationality         TEXT,
    residence_country   TEXT,
    date_of_birth       TEXT,
    risk_rating         TEXT,
    kyc_status          TEXT,
    pep_status          INTEGER DEFAULT 0,
    wallet_type         TEXT,
    created_at          TEXT,
    extra               TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS transaction_types (
    type_id   INTEGER PRIMARY KEY,
    name_en   TEXT NOT NULL,
    name_ar   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      TEXT PRIMARY KEY,
    sender_wallet_id    TEXT NOT NULL,
    receiver_wallet_id  TEXT NOT NULL,
    amount              REAL NOT NULL,
    executed_at         TEXT NOT NULL,
    transaction_type_id INTEGER NOT NULL,
    reference_number    TEXT,
    fee                 REAL DEFAULT 0,
    currency            TEXT DEFAULT 'USD',
    merchant_id         TEXT,
    merchant_name       TEXT,
    merchant_category   TEXT,
    merchant_country    TEXT,
    extra               TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tx_executed_at ON transactions(executed_at);
CREATE INDEX IF NOT EXISTS idx_tx_sender ON transactions(sender_wallet_id);
CREATE INDEX IF NOT EXISTS idx_tx_receiver ON transactions(receiver_wallet_id);

CREATE TABLE IF NOT EXISTS internal_wallets (
    wallet_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    version     INTEGER DEFAULT 1,
    definition  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    definition  TEXT NOT NULL,
    saved_at    TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS datasource_configs (
    name            TEXT PRIMARY KEY,
    es_index        TEXT NOT NULL,
    timestamp_field TEXT,
    id_field        TEXT,
    required_fields TEXT DEFAULT '[]',
    enrichments     TEXT DEFAULT '[]',
    builtin         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id             TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,
    rule_name            TEXT NOT NULL,
    rule_version         INTEGER NOT NULL,
    customer             TEXT,
    wallet_id            TEXT,
    transaction_ids      TEXT,
    risk_score           INTEGER,
    alert_severity       TEXT,
    detection_time       TEXT NOT NULL,
    rule_result          TEXT,
    investigation_status TEXT DEFAULT 'New'
);
CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts(rule_id);
"""


class Database:
    """Thread-safe wrapper around a SQLite connection."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Lightweight in-place migrations for databases created by
        earlier versions (CREATE TABLE IF NOT EXISTS skips new columns)."""
        wallet_cols = {c["name"] for c in self.query("PRAGMA table_info(wallets)")}
        if "created_at" not in wallet_cols:
            self.execute("ALTER TABLE wallets ADD COLUMN created_at TEXT")
        if "extra" not in wallet_cols:
            self.execute("ALTER TABLE wallets ADD COLUMN extra TEXT DEFAULT '{}'")

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Wallets
    # ------------------------------------------------------------------
    def upsert_wallet(self, w: Wallet) -> None:
        dob = w.date_of_birth.isoformat() if isinstance(w.date_of_birth, date) else w.date_of_birth
        self.execute(
            """INSERT OR REPLACE INTO wallets
               (wallet_id, owner_name, nationality, residence_country, date_of_birth,
                risk_rating, kyc_status, pep_status, wallet_type, created_at, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (w.wallet_id, w.owner_name, w.nationality, w.residence_country, dob,
             w.risk_rating, w.kyc_status, int(bool(w.pep_status)), w.wallet_type,
             w.created_at, json.dumps(w.extra or {})),
        )

    def _hydrate_wallet(self, row: dict) -> dict:
        row["pep_status"] = bool(row["pep_status"])
        extra = json.loads(row.pop("extra", None) or "{}")
        row.update(extra)
        return row

    def get_wallet(self, wallet_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,))
        return self._hydrate_wallet(rows[0]) if rows else None

    def list_wallets(self) -> list[dict]:
        return [self._hydrate_wallet(r) for r in
                self.query("SELECT * FROM wallets ORDER BY wallet_id")]

    # ------------------------------------------------------------------
    # Transaction types
    # ------------------------------------------------------------------
    def upsert_transaction_type(self, t: TransactionType) -> None:
        self.execute(
            "INSERT OR REPLACE INTO transaction_types (type_id, name_en, name_ar) VALUES (?,?,?)",
            (t.type_id, t.name_en, t.name_ar),
        )

    def list_transaction_types(self) -> list[dict]:
        return self.query("SELECT * FROM transaction_types ORDER BY type_id")

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    def insert_transaction(self, t: Transaction) -> None:
        executed = t.executed_at.isoformat() if isinstance(t.executed_at, datetime) else t.executed_at
        self.execute(
            """INSERT OR REPLACE INTO transactions
               (transaction_id, sender_wallet_id, receiver_wallet_id, amount, executed_at,
                transaction_type_id, reference_number, fee, currency,
                merchant_id, merchant_name, merchant_category, merchant_country, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.transaction_id, t.sender_wallet_id, t.receiver_wallet_id, t.amount, executed,
             t.transaction_type_id, t.reference_number, t.fee, t.currency,
             t.merchant_id, t.merchant_name, t.merchant_category, t.merchant_country,
             json.dumps(t.extra or {})),
        )

    def list_transactions(self, since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM transactions"
        clauses, params = [], []
        if since:
            clauses.append("executed_at >= ?")
            params.append(since)
        if until:
            clauses.append("executed_at <= ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY executed_at"
        rows = self.query(sql, params)
        for r in rows:
            extra = json.loads(r.pop("extra") or "{}")
            r.update(extra)
        return rows

    # ------------------------------------------------------------------
    # Internal wallets (settings screen)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Datasource configurations (dynamic detection sources)
    # ------------------------------------------------------------------
    def upsert_datasource_config(self, cfg: dict) -> None:
        self.execute(
            """INSERT OR REPLACE INTO datasource_configs
               (name, es_index, timestamp_field, id_field, required_fields,
                enrichments, builtin)
               VALUES (?,?,?,?,?,?,?)""",
            (cfg["name"], cfg["es_index"], cfg.get("timestamp_field"),
             cfg.get("id_field"), json.dumps(cfg.get("required_fields") or []),
             json.dumps(cfg.get("enrichments") or []), int(bool(cfg.get("builtin")))),
        )

    def _hydrate_ds_config(self, row: dict) -> dict:
        row["required_fields"] = json.loads(row["required_fields"] or "[]")
        row["enrichments"] = json.loads(row["enrichments"] or "[]")
        row["builtin"] = bool(row["builtin"])
        return row

    def list_datasource_configs(self) -> list[dict]:
        return [self._hydrate_ds_config(r) for r in
                self.query("SELECT * FROM datasource_configs ORDER BY builtin DESC, name")]

    def get_datasource_config(self, name: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM datasource_configs WHERE name = ?", (name,))
        return self._hydrate_ds_config(rows[0]) if rows else None

    def delete_datasource_config(self, name: str) -> bool:
        cur = self.execute(
            "DELETE FROM datasource_configs WHERE name = ? AND builtin = 0", (name,))
        return cur.rowcount > 0

    def upsert_internal_wallet(self, cfg: InternalWalletConfig) -> None:
        self.execute(
            "INSERT OR REPLACE INTO internal_wallets (wallet_id, name, description) VALUES (?,?,?)",
            (cfg.wallet_id, cfg.name, cfg.description),
        )

    def delete_internal_wallet(self, wallet_id: str) -> None:
        self.execute("DELETE FROM internal_wallets WHERE wallet_id = ?", (wallet_id,))

    def list_internal_wallets(self) -> list[dict]:
        return self.query("SELECT * FROM internal_wallets ORDER BY wallet_id")
