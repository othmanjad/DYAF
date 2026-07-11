"""Aggregation registry for the rule engine.

Aggregations run over the rows of one group (after group-by) and return a
single numeric value that is compared against the rule threshold.

The registry is pluggable: new aggregation types (ML scores, behavioral
metrics, ...) register themselves without touching the engine.
"""
from __future__ import annotations

import statistics
from typing import Any, Callable, Optional

from . import conditions

# name -> {fn, needs_field, description, experimental}
_REGISTRY: dict[str, dict] = {}


def register(name: str, fn: Callable, needs_field: bool = False,
             description: str = "", experimental: bool = False) -> None:
    _REGISTRY[name] = {
        "fn": fn,
        "needs_field": needs_field,
        "description": description,
        "experimental": experimental,
    }


def available() -> list[dict]:
    return [
        {"name": k, "needs_field": v["needs_field"], "description": v["description"],
         "experimental": v["experimental"]}
        for k, v in _REGISTRY.items()
    ]


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def needs_field(name: str) -> bool:
    return _REGISTRY[name]["needs_field"]


def _numeric_values(rows: list[dict], field: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(field)
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def compute(name: str, rows: list[dict], field: Optional[str] = None,
            config: Optional[dict] = None) -> float:
    """Compute aggregation `name` over `rows`. Raises on unknown type."""
    if name not in _REGISTRY:
        raise ValueError(f"Unknown aggregation type: {name}")
    return _REGISTRY[name]["fn"](rows, field, config or {})


# ----------------------------------------------------------------------
# Built-in aggregations
# ----------------------------------------------------------------------

def _agg_count(rows, field, config):
    return float(len(rows))


def _agg_distinct_count(rows, field, config):
    return float(len({str(r.get(field)) for r in rows if r.get(field) is not None}))


def _agg_sum(rows, field, config):
    return float(sum(_numeric_values(rows, field)))


def _agg_avg(rows, field, config):
    vals = _numeric_values(rows, field)
    return float(sum(vals) / len(vals)) if vals else 0.0


def _agg_min(rows, field, config):
    vals = _numeric_values(rows, field)
    return float(min(vals)) if vals else 0.0


def _agg_max(rows, field, config):
    vals = _numeric_values(rows, field)
    return float(max(vals)) if vals else 0.0


def _subset(rows, condition):
    if not condition:
        return rows
    return [r for r in rows if conditions.evaluate(condition, r)]


def _measure(rows, field):
    """count when no field given, otherwise sum of the field."""
    if field:
        return float(sum(_numeric_values(rows, field)))
    return float(len(rows))


def _agg_percentage(rows, field, config):
    """Percentage of the group matching `numerator_condition`.

    Measured by count, or by sum of `field` when a field is provided.
    Example: % of a wallet's outgoing volume sent to high-risk countries.
    """
    numerator_rows = _subset(rows, config.get("numerator_condition"))
    total = _measure(rows, field)
    if total == 0:
        return 0.0
    return _measure(numerator_rows, field) / total * 100.0


def _agg_ratio(rows, field, config):
    """Ratio between two condition subsets (numerator / denominator).

    Returns 0 when the denominator is empty — for "A exceeds B even when
    B is zero" semantics use the `difference` aggregation instead.
    """
    num = _measure(_subset(rows, config.get("numerator_condition")), field)
    den = _measure(_subset(rows, config.get("denominator_condition")), field)
    if den == 0:
        return 0.0
    return num / den


def _agg_difference(rows, field, config):
    """Difference between two condition subsets (numerator - denominator).

    Example: total debit amount minus total credit amount > 0 catches
    wallets that send more than they receive, including wallets with no
    incoming transactions at all.
    """
    num = _measure(_subset(rows, config.get("numerator_condition")), field)
    den = _measure(_subset(rows, config.get("denominator_condition")), field)
    return num - den


def _agg_stddev(rows, field, config):
    vals = _numeric_values(rows, field)
    return float(statistics.pstdev(vals)) if len(vals) >= 2 else 0.0


def _agg_moving_average(rows, field, config):
    """Moving average over the last `window_size` rows (ordered by timestamp)."""
    window = int(config.get("window_size", 5))
    ts_field = config.get("timestamp_field", "executed_at")
    ordered = sorted(rows, key=lambda r: str(r.get(ts_field, "")))
    vals = _numeric_values(ordered[-window:], field)
    return float(sum(vals) / len(vals)) if vals else 0.0


register("count", _agg_count, needs_field=False, description="Number of matching records")
register("distinct_count", _agg_distinct_count, needs_field=True, description="Distinct values of a field")
register("sum", _agg_sum, needs_field=True, description="Sum of a numeric field")
register("avg", _agg_avg, needs_field=True, description="Average of a numeric field")
register("min", _agg_min, needs_field=True, description="Minimum of a numeric field")
register("max", _agg_max, needs_field=True, description="Maximum of a numeric field")
register("percentage", _agg_percentage, needs_field=False,
         description="Percentage of the group matching a numerator condition (by count, or by sum of field)")
register("ratio", _agg_ratio, needs_field=False,
         description="Ratio between numerator and denominator condition subsets")
register("difference", _agg_difference, needs_field=False,
         description="Difference (numerator - denominator) between two condition subsets")
register("stddev", _agg_stddev, needs_field=True, experimental=True,
         description="Standard deviation of a numeric field (future support)")
register("moving_average", _agg_moving_average, needs_field=True, experimental=True,
         description="Moving average over the most recent records (future support)")
