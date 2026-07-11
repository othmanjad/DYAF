"""Scenario template library — the Word-document catalog (TM-xx) expressed
purely as SELECT statements. Nothing here is an engine feature: every
scenario is SELECT aggregates + WHERE + GROUP BY + HAVING.

TM-08 (multi-hop layering) and the fuzzy-matching half of TM-12 are graph /
screening problems that are not expressible as a single SELECT; they are
documented as future modules. The PEP/list half of TM-12 is covered.
"""
from __future__ import annotations


def _rule(rid, name, scenario, description, **kw):
    base = {
        "rule_id": rid, "name": name, "scenario_ref": scenario,
        "description": description, "from": "transactions",
        "enabled": False, "risk_score": 70, "severity": "High",
        "frequency": {"value": 1, "unit": "days"},
    }
    base.update(kw)
    return base


def scenario_templates() -> list[dict]:
    return [
        _rule(
            "TMPL-TM01", "Cash Structuring (Smurfing)", "TM-01",
            "Multiple cash movements just below the reporting threshold "
            "aggregating above it within a short window.",
            where={"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "transaction_type_en", "operator": "eq", "value": "Cash Out"},
                {"field": "amount", "operator": "between", "value": [9000, 9999]},
            ]},
            group_by=["wallet_id"],
            select=[
                {"name": "cnt", "func": "count"},
                {"name": "total", "func": "sum", "field": "amount"},
            ],
            having="cnt >= 3 AND total >= 9000",
            time_window={"value": 1, "unit": "days"},
            risk_score=90, severity="Critical",
        ),
        _rule(
            "TMPL-TM02", "Rapid Movement of Funds (Pass-Through)", "TM-02",
            "Outflow >= 80% of inflow within the window — account used as a conduit.",
            group_by=["wallet_id"],
            select=[
                {"name": "inflow", "func": "sum", "field": "amount",
                 "filter": {"field": "is_debit", "operator": "eq", "value": False}},
                {"name": "outflow", "func": "sum", "field": "amount",
                 "filter": {"field": "is_debit", "operator": "eq", "value": True}},
            ],
            having="inflow >= 5000 AND outflow >= 0.8 * inflow",
            time_window={"value": 5, "unit": "days"},
            risk_score=80,
        ),
        _rule(
            "TMPL-TM03", "Round-Amount Transactions", "TM-03",
            "Repeated perfectly round amounts (computed field: amount % 1000 == 0).",
            where={"field": "is_debit", "operator": "eq", "value": True},
            group_by=["wallet_id"],
            select=[
                {"name": "round_cnt", "func": "count",
                 "filter": {"field": "is_round", "operator": "eq", "value": True}},
            ],
            having="round_cnt >= 5",
            time_window={"value": 7, "unit": "days"},
            risk_score=60, severity="Medium",
        ),
        _rule(
            "TMPL-TM04", "High-Risk Jurisdiction Activity", "TM-04",
            "Cumulative value or count of transfers to @high_risk_countries.",
            where={"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "merchant_country", "operator": "in",
                 "value": "@high_risk_countries"},
            ]},
            group_by=["wallet_id"],
            select=[
                {"name": "total", "func": "sum", "field": "amount"},
                {"name": "cnt", "func": "count"},
            ],
            having="total > 5000 OR cnt >= 5",
            time_window={"value": 7, "unit": "days"},
            risk_score=85,
        ),
        _rule(
            "TMPL-TM05", "Velocity / Volume Spike vs Profile", "TM-05",
            "Outgoing volume exceeds 3x the customer's declared weekly volume.",
            where={"field": "is_debit", "operator": "eq", "value": True},
            group_by=["wallet_id"],
            select=[
                {"name": "total", "func": "sum", "field": "amount"},
                {"name": "declared", "func": "max",
                 "field": "wallet_expected_weekly_volume"},
            ],
            having="declared > 0 AND total > 3 * declared",
            time_window={"value": 7, "unit": "days"},
            risk_score=75,
        ),
        _rule(
            "TMPL-TM06", "Large Single Transaction vs History", "TM-06",
            "Largest transaction is big in absolute terms AND >= 5x the "
            "customer's average transaction size.",
            where={"field": "is_debit", "operator": "eq", "value": True},
            group_by=["wallet_id"],
            select=[
                {"name": "biggest", "func": "max", "field": "amount"},
                {"name": "avg_size", "func": "avg", "field": "amount"},
            ],
            having="biggest >= 10000 AND biggest >= 5 * avg_size",
            time_window={"value": 30, "unit": "days"},
            risk_score=80,
        ),
        _rule(
            "TMPL-TM07", "Dormant Account Reactivation", "TM-07",
            "Activity in the last 7 days from an account with no activity "
            "in the prior look-back period.",
            where={"field": "is_debit", "operator": "eq", "value": True},
            group_by=["wallet_id"],
            select=[
                {"name": "recent", "func": "count",
                 "filter": {"field": "executed_at", "operator": "gte", "value": "now-7d"}},
                {"name": "prior", "func": "count",
                 "filter": {"field": "executed_at", "operator": "lt", "value": "now-7d"}},
            ],
            having="recent >= 1 AND prior == 0",
            time_window={"value": 180, "unit": "days"},
            risk_score=75,
        ),
        _rule(
            "TMPL-TM09", "Many Unrelated Counterparties", "TM-09",
            "Distinct counterparties above threshold within the window.",
            where={"field": "is_debit", "operator": "eq", "value": True},
            group_by=["wallet_id"],
            select=[
                {"name": "parties", "func": "count_distinct",
                 "field": "counterparty_wallet_id"},
            ],
            having="parties >= 10",
            time_window={"value": 7, "unit": "days"},
            risk_score=65, severity="Medium",
        ),
        _rule(
            "TMPL-TM10", "New Corridor (First-Time Country)", "TM-10",
            "First-ever activity on a wallet+country corridor with meaningful "
            "value (composite GROUP BY).",
            where={"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "merchant_country", "operator": "exists"},
            ]},
            group_by=["wallet_id", "merchant_country"],
            select=[
                {"name": "recent_cnt", "func": "count",
                 "filter": {"field": "executed_at", "operator": "gte", "value": "now-7d"}},
                {"name": "prior_cnt", "func": "count",
                 "filter": {"field": "executed_at", "operator": "lt", "value": "now-7d"}},
                {"name": "recent_total", "func": "sum", "field": "amount",
                 "filter": {"field": "executed_at", "operator": "gte", "value": "now-7d"}},
            ],
            having="recent_cnt > 0 AND prior_cnt == 0 AND recent_total >= 1000",
            time_window={"value": 90, "unit": "days"},
            risk_score=70,
        ),
        _rule(
            "TMPL-TM11", "Gambling Spend Share", "TM-11",
            "Gambling merchants take >= 70% of the customer's card spend "
            "(cash-intensive / category-concentration indicator).",
            where={"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "transaction_type_en", "operator": "eq",
                 "value": "Card Payment"},
            ]},
            group_by=["wallet_id"],
            select=[
                {"name": "gambling", "func": "sum", "field": "amount",
                 "filter": {"field": "merchant_category", "operator": "in",
                            "value": "@gambling_mccs"}},
                {"name": "total", "func": "sum", "field": "amount"},
            ],
            having="total > 0 AND gambling >= 0.7 * total",
            time_window={"value": 30, "unit": "days"},
            risk_score=60, severity="Medium",
        ),
        _rule(
            "TMPL-TM12", "PEP Exposure to High-Risk Jurisdictions", "TM-12",
            "PEP customers transacting with @high_risk_countries (list-based "
            "half of TM-12; fuzzy name screening is a separate module).",
            where={"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "wallet_pep_status", "operator": "eq", "value": True},
                {"field": "merchant_country", "operator": "in",
                 "value": "@high_risk_countries"},
            ]},
            group_by=["wallet_id"],
            select=[{"name": "total", "func": "sum", "field": "amount"}],
            having="total > 5000",
            time_window={"value": 7, "unit": "days"},
            risk_score=95, severity="Critical",
        ),
    ]


DEFAULT_COMPUTED_FIELDS = [
    {"datasource": "transactions", "name": "is_round",
     "expression": "amount % 1000 == 0"},
]
