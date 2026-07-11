"""Rule executor and scheduler.

Execution pipeline (identical semantics to the generated ES DSL):

  1. fetch rows from Elasticsearch (WHERE + time window pushed down)
  2. apply computed fields (dynamic user-defined expressions)
  3. GROUP BY the (possibly composite) key
  4. compute every named SELECT aggregate (with its FILTER)
  5. evaluate the HAVING expression per group
  6. one alert per matching group (or per record in match mode)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import conditions, dsl, expr, rules
from .es_client import EsClient
from .store import Store

INVESTIGATION_STATUSES = ("New", "In Review", "Escalated",
                          "Closed - False Positive", "Closed - Confirmed")
MAX_SAMPLE_TX = 20


@dataclass
class GroupResult:
    group_key: dict
    row_count: int
    aggregates: dict
    matched: bool
    sample_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"group_key": self.group_key, "row_count": self.row_count,
                "aggregates": self.aggregates, "matched": self.matched,
                "sample_transaction_ids": self.sample_ids}


def _agg_value(func: str, rows: list[dict], fld: Optional[str]) -> float:
    if func == "count":
        return float(len(rows))
    if func == "count_distinct":
        return float(len({str(r.get(fld)) for r in rows if r.get(fld) is not None}))
    values = []
    for r in rows:
        try:
            values.append(float(r.get(fld)))
        except (TypeError, ValueError):
            continue
    if not values:
        return 0.0
    return {"sum": sum(values), "avg": sum(values) / len(values),
            "min": min(values), "max": max(values)}[func]


class Engine:
    def __init__(self, es: EsClient, store: Store):
        self.es = es
        self.store = store

    # ------------------------------------------------------------------
    def _list_resolver(self) -> conditions.ListResolver:
        return lambda name: self.store.get_list(name)

    def _computed(self, datasource: str) -> list[dict]:
        return self.store.list_computed_fields(datasource)

    def _apply_computed(self, datasource: str, rows: list[dict]) -> list[dict]:
        compiled = [(cf["name"], expr.parse(cf["expression"]))
                    for cf in self._computed(datasource)]
        for row in rows:
            for name, ast in compiled:
                try:
                    row[name] = expr.evaluate(ast, row)
                except expr.ExprError:
                    row[name] = None
        return rows

    # ------------------------------------------------------------------
    def execute(self, defn: dict, dry_run: bool = False,
                suppress_duplicates: bool = True) -> dict:
        defn = rules.normalize(defn)
        ds = self.store.get_datasource(defn["from"])
        if ds is None:
            raise ValueError(f"Unknown datasource '{defn['from']}'")
        lists = self._list_resolver()

        # push only the time window down; the WHERE tree is applied locally
        # after computed fields exist (real-cluster pushdown uses runtime
        # fields — see dsl.build — with identical semantics)
        query = dsl.base_query({**defn, "where": None}, ds.get("timestamp_field"), lists)
        raw_rows = self.es.fetch_docs(ds["es_index"], query)
        rows = self._apply_computed(defn["from"], raw_rows)
        if defn.get("where"):
            rows = [r for r in rows if conditions.evaluate(defn["where"], r, lists)]

        executed_at = datetime.now(timezone.utc).isoformat()
        group_by = defn.get("group_by") or []
        select = defn.get("select") or []
        results: list[GroupResult] = []
        alerts: list[dict] = []

        if not group_by and not select:
            # match mode: one alert per record
            for i, row in enumerate(rows):
                key = {"record": str(row.get(ds.get("id_field")) or
                                     row.get("transaction_id") or f"record-{i}")}
                results.append(GroupResult(
                    group_key=key, row_count=1, aggregates={}, matched=True,
                    sample_ids=[t for t in [row.get("transaction_id")] if t]))
                alerts.append(self._alert(defn, results[-1], [row]))
        else:
            groups: dict[tuple, list[dict]] = {}
            for row in rows:
                key = tuple(str(row.get(g)) if row.get(g) is not None else None
                            for g in group_by)
                if any(k is None for k in key):
                    continue
                groups.setdefault(key, []).append(row)

            having_ast = expr.parse(defn["having"]) if defn.get("having") else None
            for key, group_rows in sorted(groups.items()):
                agg_values = {
                    agg["name"]: round(_agg_value(
                        agg["func"],
                        [r for r in group_rows
                         if conditions.evaluate(agg.get("filter"), r, lists)]
                        if agg.get("filter") else group_rows,
                        agg.get("field")), 6)
                    for agg in select}
                matched = bool(expr.evaluate(having_ast, agg_values)) \
                    if having_ast is not None else bool(group_rows)
                key_dict = dict(zip(group_by, key))
                tx_ids = [r["transaction_id"] for r in group_rows
                          if r.get("transaction_id")]
                result = GroupResult(
                    group_key=key_dict, row_count=len(group_rows),
                    aggregates=agg_values, matched=matched,
                    sample_ids=list(dict.fromkeys(tx_ids))[:MAX_SAMPLE_TX])
                results.append(result)
                if matched:
                    alerts.append(self._alert(defn, result, group_rows))

        if not dry_run:
            open_keys = set()
            if suppress_duplicates:
                open_keys = {str(a.get("group_key"))
                             for a in self.store.list_alerts(rule_id=defn["rule_id"],
                                                             status="New")}
            for alert in alerts:
                if str(alert.get("group_key")) not in open_keys:
                    self.store.save_alert(alert)

        return {
            "rule_id": defn["rule_id"], "rule_name": defn.get("name"),
            "rule_version": defn.get("version", 1),
            "executed_at": executed_at,
            "rows_evaluated": len(rows),
            "groups_evaluated": len(results),
            "groups_matched": sum(1 for r in results if r.matched),
            "group_results": [r.to_dict() for r in results],
            "alerts": alerts, "dry_run": dry_run,
            "sql": rules.to_sql(defn),
        }

    # ------------------------------------------------------------------
    def _alert(self, defn: dict, result: GroupResult, rows: list[dict]) -> dict:
        first = rows[0] if rows else {}
        wallet_id = (result.group_key.get("wallet_id")
                     or first.get("wallet_id") or first.get("sender_wallet_id"))
        wallet = self.store.get_wallet(wallet_id) if wallet_id else None
        return {
            "alert_id": f"ALERT-{uuid.uuid4().hex[:10].upper()}",
            "rule_id": defn["rule_id"], "rule_name": defn.get("name"),
            "rule_version": defn.get("version", 1),
            "scenario_ref": defn.get("scenario_ref", ""),
            "customer": (wallet or {}).get("owner_name") or first.get("wallet_owner_name"),
            "wallet_id": wallet_id,
            "group_key": result.group_key,
            "transaction_ids": result.sample_ids,
            "risk_score": defn.get("risk_score", 50),
            "severity": defn.get("severity", "Medium"),
            "detection_time": datetime.now(timezone.utc).isoformat(),
            "investigation_status": "New",
            "rule_result": {
                "aggregates": result.aggregates,
                "having": defn.get("having", ""),
                "row_count": result.row_count,
                "record": rows[0] if (not defn.get("group_by")
                                      and not defn.get("select") and rows) else None,
            },
        }


class Scheduler:
    """Runs enabled rules on their configured frequency."""

    def __init__(self, engine: Engine, store: Store):
        self.engine = engine
        self.store = store
        self._last: dict[str, datetime] = {}

    def run_pending(self, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        out = []
        for rule in self.store.list_rules():
            if not rule.get("enabled", True):
                continue
            last = self._last.get(rule["rule_id"])
            freq = rules.window_delta(rule.get("frequency", {"value": 1, "unit": "hours"}))
            if last is None or now - last >= freq:
                out.append(self.engine.execute(rule))
                self._last[rule["rule_id"]] = now
        return out
