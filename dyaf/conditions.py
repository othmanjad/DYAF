"""WHERE-clause condition trees.

JSON structure produced by the Rule Builder, evaluated locally and
compiled to an Elasticsearch bool query:

    {"logic": "AND", "conditions": [
        {"field": "amount", "operator": "gt", "value": 1000},
        {"field": "merchant_country", "operator": "in", "value": "@high_risk_countries"},
        {"logic": "OR", "conditions": [...]}]}

Features:
* nested AND / OR / NOT groups
* datetime-aware comparisons incl. Elasticsearch date math (now-7d ...)
* named list references: a value of "@list_name" resolves through a
  list resolver (managed watchlists) both locally and in the DSL
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

LOGIC = ("AND", "OR", "NOT")
OPERATORS = {
    "eq": True, "neq": True, "gt": True, "gte": True, "lt": True, "lte": True,
    "in": True, "not_in": True, "between": True,
    "contains": True, "starts_with": True,
    "exists": False, "missing": False,
}  # name -> requires_value

_DATE_MATH = re.compile(r"^now(?:([+-])(\d+)([smhdw]))?$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

ListResolver = Callable[[str], Optional[list]]


def supported_operators() -> list[dict]:
    return [{"name": k, "requires_value": v} for k, v in OPERATORS.items()]


def parse_datetime(v) -> Optional[datetime]:
    """ISO 8601 or ES date math (now-7d). Naive values assumed UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        return None
    m = _DATE_MATH.match(v.strip())
    if m:
        now = datetime.now(timezone.utc)
        sign, num, unit = m.groups()
        if not sign:
            return now
        delta = timedelta(**{_UNITS[unit]: int(num)})
        return now - delta if sign == "-" else now + delta
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _compare(a, b) -> Optional[int]:
    """-1/0/1 ordering with datetime > numeric > string precedence."""
    da, db = parse_datetime(a), parse_datetime(b)
    if da is not None and db is not None:
        return (da > db) - (da < db)
    try:
        na, nb = float(a), float(b)
        return (na > nb) - (na < nb)
    except (TypeError, ValueError):
        pass
    if a is None or b is None:
        return None
    sa, sb = str(a), str(b)
    return (sa > sb) - (sa < sb)


def _eq(a, b) -> bool:
    if a is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        truthy = ("true", "1", "yes")
        return (str(a).lower() in truthy) == (str(b).lower() in truthy)
    c = _compare(a, b)
    return c == 0


def is_group(node) -> bool:
    return isinstance(node, dict) and "logic" in node


def _resolve_value(value, lists: Optional[ListResolver]):
    if isinstance(value, str) and value.startswith("@") and lists:
        resolved = lists(value[1:])
        if resolved is not None:
            return resolved
    return value


def evaluate(node, row: dict, lists: Optional[ListResolver] = None) -> bool:
    if node is None:
        return True
    if is_group(node):
        logic = str(node.get("logic", "AND")).upper()
        children = node.get("conditions", [])
        if not children:
            return True
        results = (evaluate(c, row, lists) for c in children)
        if logic == "AND":
            return all(results)
        if logic == "OR":
            return any(results)
        if logic == "NOT":
            return not any(results)
        raise ValueError(f"Unknown logic {logic}")

    op = node.get("operator")
    if op not in OPERATORS:
        raise ValueError(f"Unknown operator {op}")
    actual = row.get(node.get("field"))
    value = _resolve_value(node.get("value"), lists)

    if op == "exists":
        return actual is not None and actual != ""
    if op == "missing":
        return actual is None or actual == ""
    if op == "eq":
        return _eq(actual, value)
    if op == "neq":
        return not _eq(actual, value)
    if op in ("gt", "gte", "lt", "lte"):
        c = _compare(actual, value)
        if c is None:
            return False
        return {"gt": c > 0, "gte": c >= 0, "lt": c < 0, "lte": c <= 0}[op]
    if op == "between":
        if not (isinstance(value, (list, tuple)) and len(value) == 2):
            return False
        lo, hi = _compare(actual, value[0]), _compare(actual, value[1])
        return lo is not None and hi is not None and lo >= 0 and hi <= 0
    if op == "in":
        items = value if isinstance(value, list) else [value]
        return any(_eq(actual, x) for x in items)
    if op == "not_in":
        items = value if isinstance(value, list) else [value]
        return actual is not None and not any(_eq(actual, x) for x in items)
    if op == "contains":
        return actual is not None and str(value).lower() in str(actual).lower()
    if op == "starts_with":
        return actual is not None and str(actual).lower().startswith(str(value).lower())
    raise ValueError(f"Unhandled operator {op}")


def validate(node, known_fields: Optional[set] = None,
             known_lists: Optional[set] = None,
             path: str = "where", max_depth: int = 10, _depth: int = 0) -> list[str]:
    errors: list[str] = []
    if node is None:
        return errors
    if _depth > max_depth:
        return [f"{path}: nesting deeper than {max_depth}"]
    if is_group(node):
        if str(node.get("logic", "")).upper() not in LOGIC:
            errors.append(f"{path}: logic must be AND/OR/NOT")
        children = node.get("conditions", [])
        if not isinstance(children, list) or not children:
            errors.append(f"{path}: empty condition group")
            return errors
        for i, child in enumerate(children):
            errors.extend(validate(child, known_fields, known_lists,
                                   f"{path}.{i}", max_depth, _depth + 1))
        return errors
    if not isinstance(node, dict):
        return [f"{path}: condition must be an object"]
    field, op = node.get("field"), node.get("operator")
    if not field:
        errors.append(f"{path}: missing field")
    elif known_fields is not None and field not in known_fields:
        errors.append(f"{path}: unknown field '{field}'")
    if op not in OPERATORS:
        errors.append(f"{path}: unknown operator '{op}'")
    elif OPERATORS[op]:
        value = node.get("value")
        if value in (None, ""):
            errors.append(f"{path}: operator '{op}' requires a value")
        elif isinstance(value, str) and value.startswith("@"):
            if known_lists is not None and value[1:] not in known_lists:
                errors.append(f"{path}: unknown list '{value}'")
    return errors


# ----------------------------------------------------------------------
# Elasticsearch bool query compilation
# ----------------------------------------------------------------------

def to_es(node, lists: Optional[ListResolver] = None) -> dict:
    if node is None:
        return {"match_all": {}}
    if is_group(node):
        logic = str(node.get("logic", "AND")).upper()
        children = [to_es(c, lists) for c in node.get("conditions", [])]
        if logic == "AND":
            return {"bool": {"must": children}}
        if logic == "OR":
            return {"bool": {"should": children, "minimum_should_match": 1}}
        return {"bool": {"must_not": children}}

    field, op = node.get("field"), node.get("operator")
    value = _resolve_value(node.get("value"), lists)
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
        return {"bool": {"must_not": [
            {"terms": {field: value if isinstance(value, list) else [value]}}]}}
    if op == "contains":
        return {"wildcard": {field: {"value": f"*{value}*", "case_insensitive": True}}}
    if op == "starts_with":
        return {"prefix": {field: {"value": value, "case_insensitive": True}}}
    if op == "exists":
        return {"exists": {"field": field}}
    if op == "missing":
        return {"bool": {"must_not": [{"exists": {"field": field}}]}}
    raise ValueError(f"Cannot compile operator {op}")


# ----------------------------------------------------------------------
# Human-readable SQL-ish rendering (for the rule preview)
# ----------------------------------------------------------------------

def to_sql(node) -> str:
    if node is None:
        return "TRUE"
    if is_group(node):
        logic = str(node.get("logic", "AND")).upper()
        parts = [to_sql(c) for c in node.get("conditions", [])]
        if not parts:
            return "TRUE"
        if logic == "NOT":
            return "NOT (" + " OR ".join(parts) + ")"
        return "(" + f" {logic} ".join(parts) + ")"
    field, op, value = node.get("field"), node.get("operator"), node.get("value")

    def lit(v):
        if isinstance(v, str):
            return v if v.startswith("@") else f"'{v}'"
        return str(v)

    if op == "eq":
        return f"{field} = {lit(value)}"
    if op == "neq":
        return f"{field} != {lit(value)}"
    if op in ("gt", "gte", "lt", "lte"):
        sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        return f"{field} {sym} {lit(value)}"
    if op == "between":
        return f"{field} BETWEEN {lit(value[0])} AND {lit(value[1])}"
    if op == "in":
        items = value if isinstance(value, list) else [value]
        inner = value if isinstance(value, str) and str(value).startswith("@") \
            else ", ".join(lit(x) for x in items)
        return f"{field} IN ({inner})"
    if op == "not_in":
        items = value if isinstance(value, list) else [value]
        inner = value if isinstance(value, str) and str(value).startswith("@") \
            else ", ".join(lit(x) for x in items)
        return f"{field} NOT IN ({inner})"
    if op == "contains":
        return f"{field} LIKE '%{value}%'"
    if op == "starts_with":
        return f"{field} LIKE '{value}%'"
    if op == "exists":
        return f"{field} IS NOT NULL"
    if op == "missing":
        return f"{field} IS NULL"
    return f"{field} {op} {lit(value)}"
