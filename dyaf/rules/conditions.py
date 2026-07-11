"""Condition tree: nested AND / OR / NOT groups of field conditions.

A condition tree is a JSON-serializable structure built by the Rule Builder
UI. It is evaluated locally against row dicts and can also be compiled to
an Elasticsearch bool query (see query_builder).

Shape:
    {"logic": "AND", "conditions": [
        {"field": "amount", "operator": "gt", "value": 10000},
        {"logic": "OR", "conditions": [...]}
    ]}
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

LOGIC_OPERATORS = ("AND", "OR", "NOT")

# operator name -> (evaluator, requires_value)
_OPERATORS: dict[str, tuple[Callable[[Any, Any], bool], bool]] = {}


def register_operator(name: str, fn: Callable[[Any, Any], bool], requires_value: bool = True) -> None:
    """Extensibility hook: new comparison operators can be plugged in."""
    _OPERATORS[name] = (fn, requires_value)


def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_DATE_MATH = re.compile(r"^now(?:([+-])(\d+)([smhdw]))?$")
_DATE_MATH_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _to_dt(v) -> Optional[datetime]:
    """Parse ISO 8601 datetimes and Elasticsearch-style date math.

    Supports relative expressions like ``now``, ``now-7d``, ``now-24h``
    (units: s/m/h/d/w), resolved against the current UTC time — the same
    syntax Elasticsearch evaluates natively in range queries, so rules
    behave identically locally and when pushed down to the cluster.
    Naive absolute values are assumed UTC.
    """
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        m = _DATE_MATH.match(v.strip())
        if m:
            now = datetime.now(timezone.utc)
            sign, num, unit = m.groups()
            if not sign:
                return now
            delta = timedelta(**{_DATE_MATH_UNITS[unit]: int(num)})
            return now - delta if sign == "-" else now + delta
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _compare(a, b) -> Optional[int]:
    """Ordering with datetime > numeric > string precedence.

    Returns -1/0/1, or None when either side is missing.
    """
    da, db = _to_dt(a), _to_dt(b)
    if da is not None and db is not None:
        return (da > db) - (da < db)
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return (na > nb) - (na < nb)
    if a is None or b is None:
        return None
    sa, sb = str(a), str(b)
    return (sa > sb) - (sa < sb)


def _cmp(a, b, op) -> bool:
    c = _compare(a, b)
    return False if c is None else op(c, 0)


def _between(a, b) -> bool:
    if not (isinstance(b, (list, tuple)) and len(b) == 2):
        return False
    lo, hi = _compare(a, b[0]), _compare(a, b[1])
    return lo is not None and hi is not None and lo >= 0 and hi <= 0


register_operator("eq", lambda a, b: _compare(a, b) == 0 if a is not None else False)
register_operator("neq", lambda a, b: not _OPERATORS["eq"][0](a, b))
register_operator("gt", lambda a, b: _cmp(a, b, lambda c, z: c > z))
register_operator("gte", lambda a, b: _cmp(a, b, lambda c, z: c >= z))
register_operator("lt", lambda a, b: _cmp(a, b, lambda c, z: c < z))
register_operator("lte", lambda a, b: _cmp(a, b, lambda c, z: c <= z))
register_operator("in", lambda a, b: str(a) in [str(x) for x in (b if isinstance(b, list) else [b])])
register_operator("not_in", lambda a, b: not _OPERATORS["in"][0](a, b))
register_operator("contains", lambda a, b: a is not None and str(b).lower() in str(a).lower())
register_operator("starts_with", lambda a, b: a is not None and str(a).lower().startswith(str(b).lower()))
register_operator("between", _between)
register_operator("exists", lambda a, b: a is not None and a != "", requires_value=False)
register_operator("missing", lambda a, b: a is None or a == "", requires_value=False)


def supported_operators() -> list[dict]:
    return [{"name": name, "requires_value": req} for name, (_, req) in _OPERATORS.items()]


def is_group(node: dict) -> bool:
    return isinstance(node, dict) and "logic" in node


def evaluate(node: dict | None, row: dict) -> bool:
    """Evaluate a condition tree against a flat row dict."""
    if node is None:
        return True
    if is_group(node):
        logic = str(node.get("logic", "AND")).upper()
        children = node.get("conditions", [])
        if not children:
            return True
        results = (evaluate(child, row) for child in children)
        if logic == "AND":
            return all(results)
        if logic == "OR":
            return any(results)
        if logic == "NOT":
            return not any(results)
        raise ValueError(f"Unknown logic operator: {logic}")
    # Leaf condition
    op_name = node.get("operator")
    if op_name not in _OPERATORS:
        raise ValueError(f"Unknown operator: {op_name}")
    fn, _ = _OPERATORS[op_name]
    return fn(row.get(node.get("field")), node.get("value"))


def validate(node: dict | None, known_fields: set[str] | None = None,
             path: str = "root", max_depth: int = 10, _depth: int = 0) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid)."""
    errors: list[str] = []
    if node is None:
        return errors
    if _depth > max_depth:
        return [f"{path}: condition nesting exceeds max depth {max_depth}"]
    if is_group(node):
        logic = str(node.get("logic", "")).upper()
        if logic not in LOGIC_OPERATORS:
            errors.append(f"{path}: invalid logic '{node.get('logic')}' (expected AND/OR/NOT)")
        children = node.get("conditions", [])
        if not isinstance(children, list):
            errors.append(f"{path}: 'conditions' must be a list")
            return errors
        if not children:
            errors.append(f"{path}: empty condition group")
        for i, child in enumerate(children):
            errors.extend(validate(child, known_fields, f"{path}.{i}", max_depth, _depth + 1))
        return errors
    if not isinstance(node, dict):
        return [f"{path}: condition must be an object"]
    field = node.get("field")
    op = node.get("operator")
    if not field:
        errors.append(f"{path}: missing 'field'")
    elif known_fields is not None and field not in known_fields:
        errors.append(f"{path}: unknown field '{field}'")
    if op not in _OPERATORS:
        errors.append(f"{path}: unknown operator '{op}'")
    else:
        _, requires_value = _OPERATORS[op]
        if requires_value and node.get("value") in (None, ""):
            errors.append(f"{path}: operator '{op}' requires a value")
    return errors
