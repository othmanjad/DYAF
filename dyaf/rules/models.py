"""Rule definition model (requirement §6).

A rule is a JSON-serializable definition produced by the Rule Builder:

* data_source            — which datasource/index to run against
* target_entity          — Customer | Wallet | Transaction
* group_by               — field that identifies the entity instance
* execution_frequency    — how often the scheduler runs the rule
* time_window            — look-back window of data to evaluate
* conditions             — nested condition tree (filter)
* aggregation            — type + field + extra config (percentage/ratio/...)
* threshold              — operator + value applied to the aggregation output
* risk_score             — 0..100 attached to generated alerts
* alert_severity         — Low | Medium | High | Critical
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from . import aggregations, conditions

THRESHOLD_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "neq"}
TIME_UNITS = {"minutes": 1, "hours": 60, "days": 1440, "weeks": 10080}


class TargetEntity(str, enum.Enum):
    CUSTOMER = "Customer"
    WALLET = "Wallet"
    TRANSACTION = "Transaction"


class AlertSeverity(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


@dataclass
class Rule:
    name: str
    data_source: str
    target_entity: str
    group_by: str
    time_window: dict          # {"value": 24, "unit": "hours"}
    aggregation: dict          # {"type": "sum", "field": "amount", "config": {...}}
    threshold: dict            # {"operator": "gt", "value": 10000}
    risk_score: int = 50
    alert_severity: str = AlertSeverity.MEDIUM.value
    execution_frequency: dict = field(default_factory=lambda: {"value": 1, "unit": "hours"})
    conditions: Optional[dict] = None
    description: str = ""
    enabled: bool = True
    rule_id: str = field(default_factory=lambda: f"RULE-{uuid.uuid4().hex[:8].upper()}")
    version: int = 1

    # ------------------------------------------------------------------
    def time_window_delta(self) -> timedelta:
        unit = self.time_window.get("unit", "hours")
        return timedelta(minutes=float(self.time_window.get("value", 1)) * TIME_UNITS[unit])

    def frequency_delta(self) -> timedelta:
        unit = self.execution_frequency.get("unit", "hours")
        return timedelta(minutes=float(self.execution_frequency.get("value", 1)) * TIME_UNITS[unit])

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "data_source": self.data_source,
            "target_entity": self.target_entity,
            "group_by": self.group_by,
            "execution_frequency": self.execution_frequency,
            "time_window": self.time_window,
            "conditions": self.conditions,
            "aggregation": self.aggregation,
            "threshold": self.threshold,
            "risk_score": self.risk_score,
            "alert_severity": self.alert_severity,
            "enabled": self.enabled,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        kwargs = {k: v for k, v in d.items() if k in {
            "name", "description", "data_source", "target_entity", "group_by",
            "execution_frequency", "time_window", "conditions", "aggregation",
            "threshold", "risk_score", "alert_severity", "enabled", "rule_id", "version",
        } and v is not None}
        return cls(**kwargs)


def validate_rule(defn: dict, known_fields: Optional[set[str]] = None,
                  datasource_names: Optional[list[str]] = None) -> list[str]:
    """Validate a rule definition dict; returns list of errors (empty = valid)."""
    errors: list[str] = []

    if not defn.get("name", "").strip():
        errors.append("Rule name is required")

    ds = defn.get("data_source")
    if not ds:
        errors.append("data_source is required")
    elif datasource_names is not None and ds not in datasource_names:
        errors.append(f"Unknown data_source '{ds}'")

    if defn.get("target_entity") not in {e.value for e in TargetEntity}:
        errors.append("target_entity must be one of: Customer, Wallet, Transaction")

    group_by = defn.get("group_by")
    if not group_by:
        errors.append("group_by field is required")
    elif known_fields is not None and group_by not in known_fields:
        errors.append(f"Unknown group_by field '{group_by}'")

    for key in ("time_window", "execution_frequency"):
        tw = defn.get(key)
        if not isinstance(tw, dict) or tw.get("unit") not in TIME_UNITS:
            errors.append(f"{key} must be {{value, unit}} with unit in {sorted(TIME_UNITS)}")
        else:
            try:
                if float(tw.get("value", 0)) <= 0:
                    errors.append(f"{key}.value must be > 0")
            except (TypeError, ValueError):
                errors.append(f"{key}.value must be numeric")

    agg = defn.get("aggregation") or {}
    agg_type = agg.get("type")
    if not agg_type or not aggregations.is_registered(agg_type):
        errors.append(f"Unknown aggregation type '{agg_type}'")
    else:
        if aggregations.needs_field(agg_type):
            agg_field = agg.get("field")
            if not agg_field:
                errors.append(f"Aggregation '{agg_type}' requires a field")
            elif known_fields is not None and agg_field not in known_fields:
                errors.append(f"Unknown aggregation field '{agg_field}'")
        cfg = agg.get("config") or {}
        if agg_type == "ratio" and not cfg.get("numerator_condition"):
            errors.append("Aggregation 'ratio' requires config.numerator_condition")
        if agg_type == "percentage" and not cfg.get("numerator_condition"):
            errors.append("Aggregation 'percentage' requires config.numerator_condition")
        for sub in ("numerator_condition", "denominator_condition"):
            if cfg.get(sub):
                errors.extend(conditions.validate(cfg[sub], known_fields, path=f"aggregation.{sub}"))

    th = defn.get("threshold") or {}
    if th.get("operator") not in THRESHOLD_OPERATORS:
        errors.append(f"threshold.operator must be one of {sorted(THRESHOLD_OPERATORS)}")
    try:
        float(th.get("value"))
    except (TypeError, ValueError):
        errors.append("threshold.value must be numeric")

    rs = defn.get("risk_score")
    try:
        if not (0 <= int(rs) <= 100):
            errors.append("risk_score must be between 0 and 100")
    except (TypeError, ValueError):
        errors.append("risk_score must be an integer")

    if defn.get("alert_severity") not in {s.value for s in AlertSeverity}:
        errors.append("alert_severity must be one of: Low, Medium, High, Critical")

    if defn.get("conditions") is not None:
        errors.extend(conditions.validate(defn["conditions"], known_fields, path="conditions"))

    return errors


def check_threshold(value: float, threshold: dict) -> bool:
    op = threshold["operator"]
    tv = float(threshold["value"])
    return {
        "gt": value > tv, "gte": value >= tv,
        "lt": value < tv, "lte": value <= tv,
        "eq": value == tv, "neq": value != tv,
    }[op]
