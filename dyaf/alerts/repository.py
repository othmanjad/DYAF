"""Alert persistence."""
from __future__ import annotations

import json
from typing import Optional

from ..core.database import Database
from .models import Alert, InvestigationStatus


class AlertRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, alert: Alert) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO alerts
               (alert_id, rule_id, rule_name, rule_version, customer, wallet_id,
                transaction_ids, risk_score, alert_severity, detection_time,
                rule_result, investigation_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (alert.alert_id, alert.rule_id, alert.rule_name, alert.rule_version,
             alert.customer, alert.wallet_id, json.dumps(alert.transaction_ids),
             alert.risk_score, alert.alert_severity, alert.detection_time,
             json.dumps(alert.rule_result), alert.investigation_status),
        )

    def _hydrate(self, row: dict) -> dict:
        row["transaction_ids"] = json.loads(row["transaction_ids"] or "[]")
        row["rule_result"] = json.loads(row["rule_result"] or "{}")
        return row

    def list(self, rule_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM alerts", []
        clauses = []
        if rule_id:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if status:
            clauses.append("investigation_status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY detection_time DESC"
        return [self._hydrate(r) for r in self.db.query(sql, params)]

    def get(self, alert_id: str) -> Optional[dict]:
        rows = self.db.query("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
        return self._hydrate(rows[0]) if rows else None

    def update_status(self, alert_id: str, status: str) -> bool:
        valid = {s.value for s in InvestigationStatus}
        if status not in valid:
            raise ValueError(f"Invalid investigation status '{status}'. Valid: {sorted(valid)}")
        cur = self.db.execute("UPDATE alerts SET investigation_status = ? WHERE alert_id = ?",
                              (status, alert_id))
        return cur.rowcount > 0
