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

from typing import Any, Callable

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


def _cmp(a, b, op) -> bool:
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return op(na, nb)
    if a is None or b is None:
        return False
    return op(str(a), str(b))


register_operator("eq", lambda a, b: (str(a) == str(b)) if not (_to_num(a) is not None and _to_num(b) is not None) else _to_num(a) == _to_num(b))
register_operator("neq", lambda a, b: not _OPERATORS["eq"][0](a, b))
register_operator("gt", lambda a, b: _cmp(a, b, lambda x, y: x > y))
register_operator("gte", lambda a, b: _cmp(a, b, lambda x, y: x >= y))
register_operator("lt", lambda a, b: _cmp(a, b, lambda x, y: x < y))
register_operator("lte", lambda a, b: _cmp(a, b, lambda x, y: x <= y))
register_operator("in", lambda a, b: str(a) in [str(x) for x in (b if isinstance(b, list) else [b])])
register_operator("not_in", lambda a, b: not _OPERATORS["in"][0](a, b))
register_operator("contains", lambda a, b: a is not None and str(b).lower() in str(a).lower())
register_operator("starts_with", lambda a, b: a is not None and str(a).lower().startswith(str(b).lower()))
register_operator("between", lambda a, b: isinstance(b, (list, tuple)) and len(b) == 2
                  and _to_num(a) is not None and _to_num(b[0]) <= _to_num(a) <= _to_num(b[1]))
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
