"""Versioned rule persistence.

Every save bumps the rule version and archives the previous definition in
rule_versions, so alerts can always reference the exact rule version that
produced them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from ..core.database import Database
from .models import Rule


class RuleRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, rule: Rule) -> Rule:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.db.query("SELECT version, created_at FROM rules WHERE rule_id = ?",
                                 (rule.rule_id,))
        if existing:
            rule.version = existing[0]["version"] + 1
            created_at = existing[0]["created_at"]
        else:
            rule.version = rule.version or 1
            created_at = now
        definition = json.dumps(rule.to_dict())
        self.db.execute(
            """INSERT OR REPLACE INTO rules (rule_id, name, enabled, version, definition, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rule.rule_id, rule.name, int(rule.enabled), rule.version, definition, created_at, now),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO rule_versions (rule_id, version, definition, saved_at) VALUES (?,?,?,?)",
            (rule.rule_id, rule.version, definition, now),
        )
        return rule

    def get(self, rule_id: str) -> Optional[Rule]:
        rows = self.db.query("SELECT definition FROM rules WHERE rule_id = ?", (rule_id,))
        return Rule.from_dict(json.loads(rows[0]["definition"])) if rows else None

    def list(self) -> list[Rule]:
        rows = self.db.query("SELECT definition FROM rules ORDER BY name")
        return [Rule.from_dict(json.loads(r["definition"])) for r in rows]

    def versions(self, rule_id: str) -> list[dict]:
        return [
            {"version": r["version"], "saved_at": r["saved_at"],
             "definition": json.loads(r["definition"])}
            for r in self.db.query(
                "SELECT version, saved_at, definition FROM rule_versions WHERE rule_id = ? ORDER BY version",
                (rule_id,))
        ]

    def delete(self, rule_id: str) -> bool:
        cur = self.db.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
        self.db.execute("DELETE FROM rule_versions WHERE rule_id = ?", (rule_id,))
        return cur.rowcount > 0

    def set_enabled(self, rule_id: str, enabled: bool) -> Optional[Rule]:
        rule = self.get(rule_id)
        if not rule:
            return None
        rule.enabled = enabled
        # toggle does not create a new version — update in place
        definition = json.dumps(rule.to_dict())
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("UPDATE rules SET enabled = ?, definition = ?, updated_at = ? WHERE rule_id = ?",
                        (int(enabled), definition, now, rule_id))
        self.db.execute("UPDATE rule_versions SET definition = ? WHERE rule_id = ? AND version = ?",
                        (definition, rule_id, rule.version))
        return rule
