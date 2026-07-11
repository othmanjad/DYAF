"""Opening a v1-era SQLite file must not crash: old tables are archived."""
import sqlite3

from dyaf.store import Store


def _make_v1_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE wallets (
            wallet_id TEXT PRIMARY KEY, owner_name TEXT NOT NULL,
            nationality TEXT, risk_rating TEXT, pep_status INTEGER,
            wallet_type TEXT, created_at TEXT, extra TEXT DEFAULT '{}');
        INSERT INTO wallets (wallet_id, owner_name) VALUES ('W-OLD', 'Old Guy');
        CREATE TABLE rules (
            rule_id TEXT PRIMARY KEY, name TEXT, enabled INTEGER,
            version INTEGER, definition TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE alerts (
            alert_id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT,
            rule_version INTEGER, customer TEXT, wallet_id TEXT,
            transaction_ids TEXT, risk_score INTEGER, alert_severity TEXT,
            detection_time TEXT, rule_result TEXT, investigation_status TEXT);
    """)
    conn.commit()
    conn.close()


def test_v1_database_is_archived_and_v2_schema_created(tmp_path):
    db_path = str(tmp_path / "dyaf.db")
    _make_v1_db(db_path)

    store = Store(db_path)  # must not raise

    # v2 schema works
    store.upsert_wallet({"wallet_id": "W-NEW", "owner_name": "New Person"})
    assert store.get_wallet("W-NEW")["owner_name"] == "New Person"
    store.save_rule({"rule_id": "R1", "name": "r", "from": "transactions"})
    assert store.get_rule("R1")["name"] == "r"

    # v1 data preserved in backup tables
    backup = store.query("SELECT * FROM wallets_v1_backup")
    assert backup[0]["wallet_id"] == "W-OLD"


def test_reopening_v2_database_is_idempotent(tmp_path):
    db_path = str(tmp_path / "dyaf.db")
    Store(db_path).upsert_wallet({"wallet_id": "W-1", "owner_name": "x"})
    store = Store(db_path)  # reopen — no archiving, data intact
    assert store.get_wallet("W-1") is not None
