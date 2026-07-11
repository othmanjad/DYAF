"""The rule model: every detection rule IS a SELECT statement.

    {
      "rule_id": "...", "name": "...", "description": "...",
      "from": "transactions",
      "where": {<condition tree>},                # optional
      "group_by": ["wallet_id", ...],             # 0..n fields; [] => per-record
      "select": [                                 # named aggregates
        {"name": "debit_sum", "func": "sum", "field": "amount",
         "filter": {<condition tree>}},           # per-aggregate FILTER
        ...
      ],
      "having": "debit_sum > credit_sum AND tx_count >= 3",   # free expression
      "time_window": {"value": 7, "unit": "days"},
      "frequency":   {"value": 1, "unit": "hours"},
      "risk_score": 70, "severity": "High", "enabled": true,
      "scenario_ref": "TM-02"                      # optional Word-doc mapping
    }

Coverage principle: any scenario is a different combination of
SELECT/WHERE/GROUP BY/HAVING — never a new engine feature. Structuring,
pass-through ratios, dormancy (recent > 0 AND prior == 0), profile
deviation (total > 3 * max_declared), composite corridors (multi-field
GROUP BY) are all plain queries.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from . import conditions, expr

AGG_FUNCS = {
    "count": False,          # func -> needs_field
    "count_distinct": True,
    "sum": True,
    "avg": True,
    "min": True,
    "max": True,
}
SEVERITIES = ("Low", "Medium", "High", "Critical")
TIME_UNITS = {"minutes": 1, "hours": 60, "days": 1440, "weeks": 10080}
_IDENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def new_rule_id() -> str:
    return f"RULE-{uuid.uuid4().hex[:8].upper()}"


def normalize(defn: dict) -> dict:
    """Fill defaults so a stored rule is always complete."""
    d = dict(defn)
    d.setdefault("rule_id", new_rule_id())
    d.setdefault("description", "")
    d.setdefault("where", None)
    d.setdefault("group_by", [])
    d.setdefault("select", [])
    d.setdefault("having", "")
    d.setdefault("time_window", {"value": 1, "unit": "days"})
    d.setdefault("frequency", {"value": 1, "unit": "hours"})
    d.setdefault("risk_score", 50)
    d.setdefault("severity", "Medium")
    d.setdefault("enabled", True)
    d.setdefault("version", 1)
    d.setdefault("scenario_ref", "")
    return d


def window_delta(window: dict) -> timedelta:
    return timedelta(minutes=float(window.get("value", 1)) * TIME_UNITS[window.get("unit", "days")])


def is_match_mode(defn: dict) -> bool:
    """No GROUP BY and no aggregates => one alert per matching record."""
    return not defn.get("group_by") and not defn.get("select")


def validate(defn: dict, known_fields: Optional[set] = None,
             known_lists: Optional[set] = None,
             datasource_names: Optional[list] = None) -> list[str]:
    errors: list[str] = []

    if not str(defn.get("name", "")).strip():
        errors.append("name is required")

    source = defn.get("from")
    if not source:
        errors.append("'from' datasource is required")
    elif datasource_names is not None and source not in datasource_names:
        errors.append(f"unknown datasource '{source}'")

    for key in ("time_window", "frequency"):
        tw = defn.get(key)
        if not isinstance(tw, dict) or tw.get("unit") not in TIME_UNITS:
            errors.append(f"{key} must be {{value, unit}} with unit in {sorted(TIME_UNITS)}")
        else:
            try:
                if float(tw.get("value", 0)) <= 0:
                    errors.append(f"{key}.value must be > 0")
            except (TypeError, ValueError):
                errors.append(f"{key}.value must be numeric")

    try:
        if not (0 <= int(defn.get("risk_score", -1)) <= 100):
            errors.append("risk_score must be 0..100")
    except (TypeError, ValueError):
        errors.append("risk_score must be an integer")
    if defn.get("severity") not in SEVERITIES:
        errors.append(f"severity must be one of {SEVERITIES}")

    if defn.get("where") is not None:
        errors.extend(conditions.validate(defn["where"], known_fields, known_lists))

    group_by = defn.get("group_by") or []
    if not isinstance(group_by, list):
        errors.append("group_by must be a list of fields")
        group_by = []
    for g in group_by:
        if known_fields is not None and g not in known_fields:
            errors.append(f"group_by: unknown field '{g}'")

    select = defn.get("select") or []
    names = set()
    for i, agg in enumerate(select):
        name = str(agg.get("name") or "").strip()
        if not name or not _IDENT.match(name):
            errors.append(f"select[{i}]: aggregate needs a valid name (letters/digits/_)")
        elif name in names:
            errors.append(f"select[{i}]: duplicate aggregate name '{name}'")
        names.add(name)
        func = agg.get("func")
        if func not in AGG_FUNCS:
            errors.append(f"select[{i}]: unknown function '{func}' "
                          f"(available: {sorted(AGG_FUNCS)})")
        else:
            if AGG_FUNCS[func]:
                if not agg.get("field"):
                    errors.append(f"select[{i}]: {func} requires a field")
                elif known_fields is not None and agg["field"] not in known_fields:
                    errors.append(f"select[{i}]: unknown field '{agg['field']}'")
        if agg.get("filter"):
            errors.extend(conditions.validate(agg["filter"], known_fields, known_lists,
                                              path=f"select[{i}].filter"))

    having = (defn.get("having") or "").strip()
    match_mode = not group_by and not select

    if match_mode:
        if having:
            errors.append("having requires aggregates in select")
        if not defn.get("where"):
            errors.append("a rule without group_by/select (match mode) needs a where "
                          "filter, otherwise it would alert on every record")
    else:
        if not select:
            errors.append("group_by rules need at least one aggregate in select")
        if not having:
            errors.append("group_by rules need a having expression")
        else:
            try:
                ast = expr.parse(having)
                unknown = expr.variables(ast) - names
                if unknown:
                    errors.append("having references undefined aggregate(s): "
                                  + ", ".join(sorted(unknown)))
            except expr.ExprError as e:
                errors.append(f"having: {e}")

    return errors


# ----------------------------------------------------------------------
# Readable SQL rendering (rule preview / audit)
# ----------------------------------------------------------------------

def to_sql(defn: dict) -> str:
    parts = []
    select = defn.get("select") or []
    if select:
        cols = []
        for agg in select:
            func = agg.get("func", "count")
            arg = agg.get("field") if AGG_FUNCS.get(func) else "*"
            if func == "count_distinct":
                call = f"COUNT(DISTINCT {arg})"
            else:
                call = f"{func.upper()}({arg or '*'})"
            if agg.get("filter"):
                call += f" FILTER (WHERE {conditions.to_sql(agg['filter'])})"
            cols.append(f"{call} AS {agg.get('name')}")
        select_sql = ",\n       ".join((defn.get("group_by") or []) + cols)
    else:
        select_sql = "*"
    parts.append(f"SELECT {select_sql}")
    parts.append(f"FROM   {defn.get('from')}")

    tw = defn.get("time_window") or {}
    window_sql = f"{ '@timestamp' } >= now-{tw.get('value')}{ {'minutes':'m','hours':'h','days':'d','weeks':'w'}.get(tw.get('unit'),'d') }"
    where = defn.get("where")
    where_sql = conditions.to_sql(where) if where else None
    parts.append("WHERE  " + (f"{window_sql} AND {where_sql}" if where_sql else window_sql))

    if defn.get("group_by"):
        parts.append("GROUP BY " + ", ".join(defn["group_by"]))
    if (defn.get("having") or "").strip():
        parts.append(f"HAVING {defn['having'].strip()}")
    return "\n".join(parts)
