"""Rule execution engine (requirement §6 + §7).

Pipeline per rule run:
  1. Resolve the datasource and fetch rows in the rule's time window,
     filtered by the rule's condition tree.
  2. Group rows by the rule's group_by field (the target entity instance).
  3. Compute the configured aggregation per group.
  4. Compare the aggregation output against the threshold.
  5. Generate one Alert per matching group (skipped in dry-run / test mode).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..alerts.models import Alert
from ..alerts.repository import AlertRepository
from ..core.database import Database
from ..datasources.base import DataSourceRegistry
from . import aggregations
from .models import Rule, TargetEntity, check_threshold

MAX_SAMPLE_TRANSACTIONS = 20


@dataclass
class GroupResult:
    group_key: str
    row_count: int
    aggregation_value: float
    matched: bool
    sample_transaction_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "group_key": self.group_key,
            "row_count": self.row_count,
            "aggregation_value": self.aggregation_value,
            "matched": self.matched,
            "sample_transaction_ids": self.sample_transaction_ids,
        }


@dataclass
class RuleExecutionResult:
    rule_id: str
    rule_name: str
    rule_version: int
    executed_at: str
    window_start: Optional[str]
    window_end: Optional[str]
    rows_evaluated: int
    groups_evaluated: int
    groups_matched: int
    group_results: list[GroupResult]
    alerts: list[Alert]
    dry_run: bool

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "rule_version": self.rule_version,
            "executed_at": self.executed_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "rows_evaluated": self.rows_evaluated,
            "groups_evaluated": self.groups_evaluated,
            "groups_matched": self.groups_matched,
            "group_results": [g.to_dict() for g in self.group_results],
            "alerts": [a.to_dict() for a in self.alerts],
            "dry_run": self.dry_run,
        }


class RuleEngine:
    def __init__(self, datasources: DataSourceRegistry, db: Database,
                 alert_repo: Optional[AlertRepository] = None):
        self.datasources = datasources
        self.db = db
        self.alert_repo = alert_repo or AlertRepository(db)

    # ------------------------------------------------------------------
    def execute(self, rule: Rule, now: Optional[datetime] = None, dry_run: bool = False,
                suppress_duplicates: bool = True) -> RuleExecutionResult:
        now = now or datetime.now(timezone.utc)
        source = self.datasources.get(rule.data_source)

        start = end = None
        if source.timestamp_field:
            end = now
            start = now - rule.time_window_delta()

        rows = source.fetch(start=start, end=end, condition=rule.conditions)

        # Match mode (no aggregation): each record — or each group_by entity
        # when one is given — alerts directly. Internally this is count >= 1.
        match_mode = not (rule.aggregation or {}).get("type")
        agg = rule.aggregation if not match_mode else {"type": "count"}
        threshold = rule.threshold or ({"operator": "gte", "value": 1} if match_mode else None)

        groups: dict[str, list[dict]] = {}
        if rule.group_by:
            for row in rows:
                key = row.get(rule.group_by)
                if key is None:
                    continue
                groups.setdefault(str(key), []).append(row)
        else:  # per-record grouping (match mode without group_by)
            for i, row in enumerate(rows):
                key = row.get("transaction_id") or row.get("wallet_id") or f"record-{i}"
                groups.setdefault(str(key), []).append(row)
        group_results: list[GroupResult] = []
        alerts: list[Alert] = []

        open_keys: set[str] = set()
        if suppress_duplicates and not dry_run:
            for existing in self.alert_repo.list(rule_id=rule.rule_id, status="New"):
                open_keys.add(str((existing.get("rule_result") or {}).get("group_key")))

        for key, group_rows in sorted(groups.items()):
            value = aggregations.compute(agg.get("type", "count"), group_rows,
                                         field=agg.get("field"), config=agg.get("config"))
            matched = check_threshold(value, threshold)
            tx_ids = [r["transaction_id"] for r in group_rows if r.get("transaction_id")]
            result = GroupResult(
                group_key=key, row_count=len(group_rows), aggregation_value=round(value, 6),
                matched=matched, sample_transaction_ids=tx_ids[:MAX_SAMPLE_TRANSACTIONS],
            )
            group_results.append(result)
            if matched:
                alert = self._build_alert(rule, result, group_rows,
                                          match_mode=match_mode, threshold=threshold)
                alerts.append(alert)
                if not dry_run and key not in open_keys:
                    self.alert_repo.save(alert)

        return RuleExecutionResult(
            rule_id=rule.rule_id, rule_name=rule.name, rule_version=rule.version,
            executed_at=now.isoformat(),
            window_start=start.isoformat() if start else None,
            window_end=end.isoformat() if end else None,
            rows_evaluated=len(rows), groups_evaluated=len(groups),
            groups_matched=sum(1 for g in group_results if g.matched),
            group_results=group_results, alerts=alerts, dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    def _resolve_entity(self, rule: Rule, group_key: str, rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
        """Resolve (customer_name, wallet_id) for the alert from the group."""
        first = rows[0] if rows else {}
        if rule.target_entity == TargetEntity.WALLET.value:
            wallet = self.db.get_wallet(group_key)
            if wallet:
                return (wallet["owner_name"], group_key)
            # group key is not a wallet id (e.g. per-record match rule):
            # fall back to the sending wallet of the matched rows
            wallet_id = first.get("sender_wallet_id") or first.get("wallet_id")
            wallet = self.db.get_wallet(wallet_id) if wallet_id else None
            return (wallet["owner_name"] if wallet else None, wallet_id)
        if rule.target_entity == TargetEntity.CUSTOMER.value:
            # group key is a customer attribute; derive a wallet from the rows
            wallet_id = None
            for cand in ("sender_wallet_id", "wallet_id", "receiver_wallet_id"):
                if first.get(cand):
                    wallet_id = first[cand]
                    break
            return (group_key, wallet_id)
        # Transaction-level rule
        wallet_id = first.get("sender_wallet_id") or first.get("wallet_id")
        wallet = self.db.get_wallet(wallet_id) if wallet_id else None
        return (wallet["owner_name"] if wallet else None, wallet_id)

    def _build_alert(self, rule: Rule, result: GroupResult, rows: list[dict],
                     match_mode: bool = False, threshold: Optional[dict] = None) -> Alert:
        customer, wallet_id = self._resolve_entity(rule, result.group_key, rows)
        rule_result = {
            "mode": "match" if match_mode else "aggregate",
            "group_key": result.group_key,
            "group_by": rule.group_by,
            "aggregation": rule.aggregation,
            "aggregation_value": result.aggregation_value,
            "threshold": rule.threshold if not match_mode else None,
            "row_count": result.row_count,
        }
        if match_mode and len(rows) == 1:
            # snapshot of the matched record for direct investigation
            rule_result["record"] = rows[0]
        return Alert(
            rule_id=rule.rule_id, rule_name=rule.name, rule_version=rule.version,
            risk_score=rule.risk_score, alert_severity=rule.alert_severity,
            customer=customer, wallet_id=wallet_id,
            transaction_ids=result.sample_transaction_ids,
            rule_result=rule_result,
        )
