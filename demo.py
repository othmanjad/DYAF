"""End-to-end demo: seed the platform, define AML/Fraud rules, run the
scheduler and show the generated alerts.

    python demo.py
"""
from __future__ import annotations

import json

from dyaf.alerts.repository import AlertRepository
from dyaf.core.database import Database
from dyaf.datasources.base import DataSourceRegistry
from dyaf.datasources.sqlite_source import TransactionsDataSource, WalletsDataSource
from dyaf.rules.engine import RuleEngine
from dyaf.rules.models import Rule, validate_rule
from dyaf.rules.query_builder import build_rule_query
from dyaf.rules.repository import RuleRepository
from dyaf.scheduler import RuleScheduler
from dyaf.seed import seed


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    db = Database(":memory:")
    counts = seed(db)
    hr("1) Platform seeded")
    print(json.dumps(counts, indent=2))

    registry = DataSourceRegistry()
    registry.register(TransactionsDataSource(db))
    registry.register(WalletsDataSource(db))

    hr("2) Dynamic field discovery (no hardcoded fields)")
    fields = registry.get("transactions").get_fields()
    print(f"transactions datasource exposes {len(fields)} fields, e.g.:")
    print(", ".join(f.name for f in fields[:14]) + ", ...")

    rules_repo = RuleRepository(db)
    alerts_repo = AlertRepository(db)
    engine = RuleEngine(registry, db, alerts_repo)
    scheduler = RuleScheduler(engine, rules_repo)

    rules = [
        Rule(
            name="Structuring Detection",
            description="5+ cash-outs between 9,000 and 9,999 within 24h",
            data_source="transactions", target_entity="Wallet", group_by="sender_wallet_id",
            time_window={"value": 24, "unit": "hours"},
            execution_frequency={"value": 1, "unit": "hours"},
            conditions={"logic": "AND", "conditions": [
                {"field": "amount", "operator": "between", "value": [9000, 9999]},
                {"field": "transaction_type_en", "operator": "eq", "value": "Cash Out"},
            ]},
            aggregation={"type": "count"},
            threshold={"operator": "gte", "value": 5},
            risk_score=90, alert_severity="Critical",
        ),
        Rule(
            name="Large Single Transfer",
            description="Any transaction >= 50,000 in the last 7 days",
            data_source="transactions", target_entity="Transaction", group_by="transaction_id",
            time_window={"value": 7, "unit": "days"},
            aggregation={"type": "max", "field": "amount"},
            threshold={"operator": "gte", "value": 50000},
            risk_score=80, alert_severity="High",
        ),
        Rule(
            name="PEP Remittances to High-Risk Countries",
            description="PEP customer sending > 5,000 to FATF high-risk countries in 7 days",
            data_source="transactions", target_entity="Customer", group_by="sender_owner_name",
            time_window={"value": 7, "unit": "days"},
            conditions={"logic": "AND", "conditions": [
                {"field": "transaction_type_en", "operator": "eq", "value": "International Remittance"},
                {"field": "merchant_country", "operator": "in", "value": ["IR", "KP", "SY", "MM"]},
                {"field": "sender_pep_status", "operator": "eq", "value": 1},
            ]},
            aggregation={"type": "sum", "field": "amount"},
            threshold={"operator": "gt", "value": 5000},
            risk_score=95, alert_severity="Critical",
        ),
        Rule(
            name="Gambling Spend Share",
            description="More than 70% of card spend volume at gambling merchants",
            data_source="transactions", target_entity="Wallet", group_by="sender_wallet_id",
            time_window={"value": 7, "unit": "days"},
            conditions={"field": "transaction_type_en", "operator": "eq", "value": "Card Payment"},
            aggregation={"type": "percentage", "field": "amount",
                         "config": {"numerator_condition":
                                    {"field": "merchant_category", "operator": "eq", "value": "Gambling"}}},
            threshold={"operator": "gt", "value": 70},
            risk_score=60, alert_severity="Medium",
        ),
    ]

    hr("3) Validating and saving rules (versioned)")
    known = registry.get("transactions").field_names()
    for rule in rules:
        errors = validate_rule(rule.to_dict(), known_fields=known, datasource_names=registry.names())
        assert not errors, errors
        rules_repo.save(rule)
        print(f"  [OK] {rule.name}  (v{rule.version}, {rule.alert_severity}, risk={rule.risk_score})")

    hr("4) Elasticsearch query preview for 'Structuring Detection'")
    print(json.dumps(build_rule_query(rules[0].to_dict()), indent=2)[:1400])

    hr("5) Scheduler run — executing all due rules")
    results = scheduler.run_pending()
    for r in results:
        print(f"  {r.rule_name}: {r.rows_evaluated} rows, {r.groups_evaluated} entities, "
              f"{r.groups_matched} matched")

    hr("6) Generated alerts")
    for a in alerts_repo.list():
        rr = a["rule_result"]
        print(f"  {a['alert_id']}  [{a['alert_severity']:<8}] score={a['risk_score']:<3} "
              f"rule='{a['rule_name']}' v{a['rule_version']}")
        print(f"      customer={a['customer']}, wallet={a['wallet_id']}, "
              f"value={rr['aggregation_value']} (threshold {rr['threshold']['operator']} "
              f"{rr['threshold']['value']}), txs={len(a['transaction_ids'])}, "
              f"status={a['investigation_status']}")

    hr("7) Investigation workflow")
    first = alerts_repo.list()[0]
    alerts_repo.update_status(first["alert_id"], "In Review")
    alerts_repo.update_status(first["alert_id"], "Closed - Confirmed")
    print(f"  {first['alert_id']} -> {alerts_repo.get(first['alert_id'])['investigation_status']}")

    total = len(alerts_repo.list())
    print(f"\nDemo complete: {total} alerts generated across {len(rules)} rules.")


if __name__ == "__main__":
    main()
