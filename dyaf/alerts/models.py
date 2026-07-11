"""Alert model (requirement §7)."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class InvestigationStatus(str, enum.Enum):
    NEW = "New"
    IN_REVIEW = "In Review"
    ESCALATED = "Escalated"
    CLOSED_FALSE_POSITIVE = "Closed - False Positive"
    CLOSED_CONFIRMED = "Closed - Confirmed"


@dataclass
class Alert:
    rule_id: str
    rule_name: str
    rule_version: int
    risk_score: int
    alert_severity: str
    rule_result: dict                       # aggregation value, threshold, group key...
    customer: Optional[str] = None          # customer (wallet owner) name
    wallet_id: Optional[str] = None
    transaction_ids: list = field(default_factory=list)
    detection_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    investigation_status: str = InvestigationStatus.NEW.value
    alert_id: str = field(default_factory=lambda: f"ALERT-{uuid.uuid4().hex[:10].upper()}")

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "rule_version": self.rule_version,
            "customer": self.customer,
            "wallet_id": self.wallet_id,
            "transaction_ids": self.transaction_ids,
            "risk_score": self.risk_score,
            "alert_severity": self.alert_severity,
            "detection_time": self.detection_time,
            "rule_result": self.rule_result,
            "investigation_status": self.investigation_status,
        }
