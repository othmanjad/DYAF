"""SQLite operational store.

Elasticsearch is the only read path for detection queries. SQLite keeps
the platform's operational data: wallet master records (join source),
transaction types, internal wallet classification, rule definitions with
versions, alerts, named lists, computed fields, join definitions and
datasource configurations.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    wallet_id  TEXT PRIMARY KEY,
    data       TEXT NOT NULL          -- full wallet document (dynamic fields)
);

CREATE TABLE IF NOT EXISTS transaction_types (
    type_id  INTEGER PRIMARY KEY,
    name_en  TEXT NOT NULL,
    name_ar  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_wallets (
    wallet_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS datasources (
    name   TEXT PRIMARY KEY,
    config TEXT NOT NULL               -- es_index, timestamp_field, id_field,
                                       -- required_fields, joins, builtin
);

CREATE TABLE IF NOT EXISTS computed_fields (
    datasource TEXT NOT NULL,
    name       TEXT NOT NULL,
    expression TEXT NOT NULL,
    PRIMARY KEY (datasource, name)
);

CREATE TABLE IF NOT EXISTS lists (
    name   TEXT PRIMARY KEY,
    values_json TEXT NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    version    INTEGER DEFAULT 1,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    definition TEXT NOT NULL,
    saved_at   TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id             TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,
    data                 TEXT NOT NULL,
    detection_time       TEXT NOT NULL,
    investigation_status TEXT DEFAULT 'New'
);
CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts(rule_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._archive_v1_schema()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _archive_v1_schema(self) -> None:
        """Opening a v1 database: archive the old tables so the v2 schema
        can be created cleanly (v1 kept as *_v1_backup for reference)."""
        tables = {r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "wallets" not in tables:
            return
        cols = {c["name"] for c in self.query("PRAGMA table_info(wallets)")}
        if "data" in cols:
            return  # already v2
        for t in ("wallets", "transactions", "transaction_types",
                  "internal_wallets", "rules", "rule_versions", "alerts",
                  "datasource_configs"):
            if t in tables:
                self.execute(f"DROP TABLE IF EXISTS {t}_v1_backup")
                self.execute(f"ALTER TABLE {t} RENAME TO {t}_v1_backup")

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    # -------------------------------------------------- wallets (join master)
    def upsert_wallet(self, doc: dict) -> None:
        wallet_id = doc.get("wallet_id")
        if not wallet_id:
            raise ValueError("wallet document requires wallet_id")
        self.execute("INSERT OR REPLACE INTO wallets (wallet_id, data) VALUES (?,?)",
                     (wallet_id, json.dumps(doc)))

    def get_wallet(self, wallet_id: str) -> Optional[dict]:
        rows = self.query("SELECT data FROM wallets WHERE wallet_id=?", (wallet_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def list_wallets(self) -> list[dict]:
        return [json.loads(r["data"]) for r in
                self.query("SELECT data FROM wallets ORDER BY wallet_id")]

    # -------------------------------------------------- reference tables
    def upsert_transaction_type(self, type_id: int, name_en: str, name_ar: str) -> None:
        self.execute("INSERT OR REPLACE INTO transaction_types VALUES (?,?,?)",
                     (type_id, name_en, name_ar))

    def list_transaction_types(self) -> list[dict]:
        return self.query("SELECT * FROM transaction_types ORDER BY type_id")

    def upsert_internal_wallet(self, wallet_id: str, name: str, description: str = "") -> None:
        self.execute("INSERT OR REPLACE INTO internal_wallets VALUES (?,?,?)",
                     (wallet_id, name, description))

    def delete_internal_wallet(self, wallet_id: str) -> None:
        self.execute("DELETE FROM internal_wallets WHERE wallet_id=?", (wallet_id,))

    def list_internal_wallets(self) -> list[dict]:
        return self.query("SELECT * FROM internal_wallets ORDER BY wallet_id")

    # -------------------------------------------------- datasources
    def upsert_datasource(self, name: str, config: dict) -> None:
        self.execute("INSERT OR REPLACE INTO datasources VALUES (?,?)",
                     (name, json.dumps(config)))

    def get_datasource(self, name: str) -> Optional[dict]:
        rows = self.query("SELECT config FROM datasources WHERE name=?", (name,))
        return json.loads(rows[0]["config"]) if rows else None

    def list_datasources(self) -> list[dict]:
        return [json.loads(r["config"]) for r in
                self.query("SELECT config FROM datasources ORDER BY name")]

    def delete_datasource(self, name: str) -> bool:
        return self.execute("DELETE FROM datasources WHERE name=?", (name,)).rowcount > 0

    # -------------------------------------------------- computed fields
    def upsert_computed_field(self, datasource: str, name: str, expression: str) -> None:
        self.execute("INSERT OR REPLACE INTO computed_fields VALUES (?,?,?)",
                     (datasource, name, expression))

    def delete_computed_field(self, datasource: str, name: str) -> bool:
        return self.execute("DELETE FROM computed_fields WHERE datasource=? AND name=?",
                            (datasource, name)).rowcount > 0

    def list_computed_fields(self, datasource: Optional[str] = None) -> list[dict]:
        if datasource:
            return self.query("SELECT * FROM computed_fields WHERE datasource=? ORDER BY name",
                              (datasource,))
        return self.query("SELECT * FROM computed_fields ORDER BY datasource, name")

    # -------------------------------------------------- named lists
    def upsert_list(self, name: str, values: list, description: str = "") -> None:
        self.execute("INSERT OR REPLACE INTO lists VALUES (?,?,?)",
                     (name, json.dumps(values), description))

    def get_list(self, name: str) -> Optional[list]:
        rows = self.query("SELECT values_json FROM lists WHERE name=?", (name,))
        return json.loads(rows[0]["values_json"]) if rows else None

    def list_lists(self) -> list[dict]:
        return [{"name": r["name"], "values": json.loads(r["values_json"]),
                 "description": r["description"]}
                for r in self.query("SELECT * FROM lists ORDER BY name")]

    def delete_list(self, name: str) -> bool:
        return self.execute("DELETE FROM lists WHERE name=?", (name,)).rowcount > 0

    # -------------------------------------------------- rules (versioned)
    def save_rule(self, definition: dict) -> dict:
        rule_id = definition["rule_id"]
        now = utcnow()
        existing = self.query("SELECT version, created_at FROM rules WHERE rule_id=?",
                              (rule_id,))
        if existing:
            definition["version"] = existing[0]["version"] + 1
            created = existing[0]["created_at"]
        else:
            definition["version"] = definition.get("version") or 1
            created = now
        payload = json.dumps(definition)
        self.execute(
            "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?)",
            (rule_id, definition.get("name", rule_id),
             int(bool(definition.get("enabled", True))),
             definition["version"], payload, created, now))
        self.execute(
            "INSERT OR REPLACE INTO rule_versions VALUES (?,?,?,?)",
            (rule_id, definition["version"], payload, now))
        return definition

    def get_rule(self, rule_id: str) -> Optional[dict]:
        rows = self.query("SELECT definition FROM rules WHERE rule_id=?", (rule_id,))
        return json.loads(rows[0]["definition"]) if rows else None

    def list_rules(self) -> list[dict]:
        return [json.loads(r["definition"]) for r in
                self.query("SELECT definition FROM rules ORDER BY name")]

    def rule_versions(self, rule_id: str) -> list[dict]:
        return [{"version": r["version"], "saved_at": r["saved_at"],
                 "definition": json.loads(r["definition"])}
                for r in self.query(
                    "SELECT * FROM rule_versions WHERE rule_id=? ORDER BY version",
                    (rule_id,))]

    def delete_rule(self, rule_id: str) -> bool:
        ok = self.execute("DELETE FROM rules WHERE rule_id=?", (rule_id,)).rowcount > 0
        self.execute("DELETE FROM rule_versions WHERE rule_id=?", (rule_id,))
        return ok

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> Optional[dict]:
        rule = self.get_rule(rule_id)
        if not rule:
            return None
        rule["enabled"] = enabled
        payload = json.dumps(rule)
        self.execute("UPDATE rules SET enabled=?, definition=?, updated_at=? WHERE rule_id=?",
                     (int(enabled), payload, utcnow(), rule_id))
        self.execute("UPDATE rule_versions SET definition=? WHERE rule_id=? AND version=?",
                     (payload, rule_id, rule["version"]))
        return rule

    # -------------------------------------------------- alerts
    def save_alert(self, alert: dict) -> None:
        self.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?)",
                     (alert["alert_id"], alert["rule_id"], json.dumps(alert),
                      alert["detection_time"], alert["investigation_status"]))

    def get_alert(self, alert_id: str) -> Optional[dict]:
        rows = self.query("SELECT data, investigation_status FROM alerts WHERE alert_id=?",
                          (alert_id,))
        if not rows:
            return None
        alert = json.loads(rows[0]["data"])
        alert["investigation_status"] = rows[0]["investigation_status"]
        return alert

    def list_alerts(self, rule_id: Optional[str] = None,
                    status: Optional[str] = None) -> list[dict]:
        sql, params, clauses = "SELECT data, investigation_status FROM alerts", [], []
        if rule_id:
            clauses.append("rule_id=?")
            params.append(rule_id)
        if status:
            clauses.append("investigation_status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY detection_time DESC"
        out = []
        for r in self.query(sql, params):
            alert = json.loads(r["data"])
            alert["investigation_status"] = r["investigation_status"]
            out.append(alert)
        return out

    def set_alert_status(self, alert_id: str, status: str) -> bool:
        return self.execute("UPDATE alerts SET investigation_status=? WHERE alert_id=?",
                            (status, alert_id)).rowcount > 0
