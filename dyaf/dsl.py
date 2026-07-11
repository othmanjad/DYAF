"""Compile a SELECT rule into a full Elasticsearch request body.

Mapping of the rule model onto native ES constructs:

    WHERE + time window     -> bool query (+ range on the timestamp field)
    computed fields         -> runtime_mappings (Painless)
    GROUP BY f1, f2         -> composite aggregation sources
    SELECT name=FUNC FILTER -> per-name filter agg wrapping a metric agg
    HAVING expression       -> bucket_selector with the compiled script

The exact same rule is evaluated by the local executor with identical
semantics, so preview / pushdown / dev-mode always agree.
"""
from __future__ import annotations

from typing import Optional

from . import conditions, expr
from .rules import AGG_FUNCS

_ES_METRIC = {"sum": "sum", "avg": "avg", "min": "min", "max": "max",
              "count_distinct": "cardinality"}
_UNIT_ABBR = {"minutes": "m", "hours": "h", "days": "d", "weeks": "w"}


def _runtime_mappings(computed: list[dict]) -> dict:
    out = {}
    for cf in computed or []:
        ast = expr.parse(cf["expression"])
        script = expr.to_painless(
            ast, var_render=lambda f: f"doc['{f}'].value")
        out[cf["name"]] = {"type": "double", "script": {"source": f"emit({script})"}}
    return out


def base_query(defn: dict, timestamp_field: Optional[str],
               lists: Optional[conditions.ListResolver] = None) -> dict:
    must = []
    if defn.get("where"):
        must.append(conditions.to_es(defn["where"], lists))
    tw = defn.get("time_window") or {}
    if timestamp_field and tw:
        abbr = _UNIT_ABBR.get(tw.get("unit", "days"), "d")
        must.append({"range": {timestamp_field: {
            "gte": f"now-{tw.get('value', 1)}{abbr}", "lte": "now"}}})
    return {"bool": {"must": must}} if must else {"match_all": {}}


def _aggregate_aggs(select: list[dict],
                    lists: Optional[conditions.ListResolver]) -> dict:
    aggs = {}
    for agg in select:
        name, func = agg["name"], agg["func"]
        node: dict = {"filter": conditions.to_es(agg.get("filter"), lists)
                      if agg.get("filter") else {"match_all": {}}}
        if func != "count":
            node["aggs"] = {"m": {_ES_METRIC[func]: {"field": agg.get("field")}}}
        aggs[name] = node
    return aggs


def _buckets_path(select: list[dict]) -> dict:
    return {agg["name"]: f"{agg['name']}>" + ("_count" if agg["func"] == "count" else "m")
            for agg in select}


def build(defn: dict, timestamp_field: Optional[str] = "executed_at",
          computed_fields: Optional[list[dict]] = None,
          lists: Optional[conditions.ListResolver] = None) -> dict:
    """Full ES search body for a rule (preview + pushdown)."""
    body: dict = {"query": base_query(defn, timestamp_field, lists)}
    runtime = _runtime_mappings(computed_fields or [])
    if runtime:
        body["runtime_mappings"] = runtime

    group_by = defn.get("group_by") or []
    select = defn.get("select") or []

    if not group_by and not select:      # match mode: return the records
        body["size"] = 10000
        return body

    body["size"] = 0
    inner: dict = _aggregate_aggs(select, lists)
    having = (defn.get("having") or "").strip()
    if having:
        ast = expr.parse(having)
        inner["having"] = {"bucket_selector": {
            "buckets_path": _buckets_path(select),
            "script": expr.to_painless(ast)}}

    body["aggs"] = {"by_entity": {
        "composite": {
            "size": 10000,
            "sources": [{g: {"terms": {"field": g}}} for g in group_by],
        },
        "aggs": inner,
    }}
    return body
