"""Compile a rule definition into an Elasticsearch DSL query.

Used for the Rule Builder's "Query Preview" and pushed down by the
Elasticsearch datasource adapter. The same rule structure is evaluated
locally by the reference SQLite datasource, so previews always reflect
exactly what would run against the cluster.
"""
from __future__ import annotations

from typing import Optional

from .conditions import is_group

_AGG_METRIC_MAP = {"sum": "sum", "avg": "avg", "min": "min", "max": "max",
                   "stddev": "extended_stats", "distinct_count": "cardinality"}


def _leaf_to_es(cond: dict) -> dict:
    field, op, value = cond.get("field"), cond.get("operator"), cond.get("value")
    if op == "eq":
        return {"term": {field: value}}
    if op == "neq":
        return {"bool": {"must_not": [{"term": {field: value}}]}}
    if op in ("gt", "gte", "lt", "lte"):
        return {"range": {field: {op: value}}}
    if op == "between":
        return {"range": {field: {"gte": value[0], "lte": value[1]}}}
    if op == "in":
        return {"terms": {field: value if isinstance(value, list) else [value]}}
    if op == "not_in":
        return {"bool": {"must_not": [{"terms": {field: value if isinstance(value, list) else [value]}}]}}
    if op == "contains":
        return {"wildcard": {field: {"value": f"*{value}*", "case_insensitive": True}}}
    if op == "starts_with":
        return {"prefix": {field: {"value": value, "case_insensitive": True}}}
    if op == "exists":
        return {"exists": {"field": field}}
    if op == "missing":
        return {"bool": {"must_not": [{"exists": {"field": field}}]}}
    raise ValueError(f"Cannot compile operator '{op}' to Elasticsearch DSL")


def build_bool_query(node: Optional[dict]) -> dict:
    """Compile a condition tree into an Elasticsearch bool query."""
    if node is None:
        return {"match_all": {}}
    if not is_group(node):
        return _leaf_to_es(node)
    logic = str(node.get("logic", "AND")).upper()
    children = [build_bool_query(c) for c in node.get("conditions", [])]
    if logic == "AND":
        return {"bool": {"must": children}}
    if logic == "OR":
        return {"bool": {"should": children, "minimum_should_match": 1}}
    if logic == "NOT":
        return {"bool": {"must_not": children}}
    raise ValueError(f"Unknown logic operator: {logic}")


def _metric_agg(agg: dict) -> Optional[dict]:
    agg_type, field = agg.get("type"), agg.get("field")
    cfg = agg.get("config") or {}
    if agg_type == "count":
        return None  # bucket doc_count is the metric
    if agg_type in _AGG_METRIC_MAP:
        return {"metric": {_AGG_METRIC_MAP[agg_type]: {"field": field}}}
    if agg_type == "percentage":
        measure = {"sum": {"field": field}} if field else None
        numerator: dict = {"filter": build_bool_query(cfg.get("numerator_condition"))}
        total_key = "doc_count"
        aggs: dict = {"numerator": numerator}
        if measure:
            numerator["aggs"] = {"measure": measure}
            aggs["total_measure"] = measure
            script = "params.num / params.total * 100"
            buckets = {"num": "numerator>measure", "total": "total_measure"}
        else:
            script = "params.num / params.total * 100"
            buckets = {"num": "numerator>_count", "total": "_count"}
        aggs["metric"] = {"bucket_script": {"buckets_path": buckets, "script": script}}
        return aggs
    if agg_type in ("ratio", "difference"):
        measure_of = ({"sum": {"field": field}} if field else None)
        num: dict = {"filter": build_bool_query(cfg.get("numerator_condition"))}
        den: dict = {"filter": build_bool_query(cfg.get("denominator_condition"))}
        if measure_of:
            num["aggs"] = {"measure": measure_of}
            den["aggs"] = {"measure": dict(measure_of)}
            paths = {"num": "numerator>measure", "den": "denominator>measure"}
        else:
            paths = {"num": "numerator>_count", "den": "denominator>_count"}
        script = "params.num / params.den" if agg_type == "ratio" else "params.num - params.den"
        return {
            "numerator": num,
            "denominator": den,
            "metric": {"bucket_script": {"buckets_path": paths, "script": script}},
        }
    if agg_type == "compare":
        def side_aggs(side: dict) -> dict:
            side = side or {}
            node: dict = {"filter": build_bool_query(side.get("condition"))}
            stype = side.get("type", "count")
            if stype != "count":
                es_metric = _AGG_METRIC_MAP.get(stype, stype)
                node["aggs"] = {"measure": {es_metric: {"field": side.get("field")}}}
            return node

        left, right = cfg.get("left") or {}, cfg.get("right") or {}
        paths = {
            "l": "left>" + ("_count" if left.get("type", "count") == "count" else "measure"),
            "r": "right>" + ("_count" if right.get("type", "count") == "count" else "measure"),
        }
        operation = cfg.get("operation", "subtract")
        script = {
            "subtract": "params.l - params.r",
            "divide": "params.l / params.r",
            "left_when_right_zero": "params.r == 0 ? params.l : 0",
        }.get(operation, "params.l - params.r")
        return {
            "left": side_aggs(left),
            "right": side_aggs(right),
            "metric": {"bucket_script": {"buckets_path": paths, "script": script}},
        }
    if agg_type == "moving_average":
        return {"metric": {"avg": {"field": field}},
                "_comment": "moving_average is computed over a date_histogram in streaming mode (future support)"}
    raise ValueError(f"Cannot compile aggregation '{agg_type}' to Elasticsearch DSL")


def build_rule_query(rule_defn: dict, timestamp_field: Optional[str] = "executed_at") -> dict:
    """Full ES search body for a rule.

    Aggregate mode: filter + time window + group-by terms agg + metric.
    Match mode (no aggregation): filter + time window returning the
    matching documents themselves.
    """
    must = []
    if rule_defn.get("conditions"):
        must.append(build_bool_query(rule_defn["conditions"]))
    tw = rule_defn.get("time_window") or {}
    if timestamp_field and tw:
        unit_abbrev = {"minutes": "m", "hours": "h", "days": "d", "weeks": "w"}.get(tw.get("unit", "hours"), "h")
        must.append({"range": {timestamp_field: {"gte": f"now-{tw.get('value', 1)}{unit_abbrev}", "lte": "now"}}})

    query = {"bool": {"must": must}} if must else {"match_all": {}}

    agg = rule_defn.get("aggregation") or {}
    if not agg.get("type"):  # match mode — return the matching records
        return {"size": 10000, "query": query}

    group_agg: dict = {"terms": {"field": rule_defn.get("group_by"), "size": 10000}}
    metric = _metric_agg(agg)
    if metric:
        group_agg["aggs"] = metric

    return {
        "size": 0,
        "query": query,
        "aggs": {"by_entity": group_agg},
    }
