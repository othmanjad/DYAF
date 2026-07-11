"""End-to-end demo — Elasticsearch edition.

Connects to the cluster at ELASTICSEARCH_URL (starts the bundled dev/test
double when unset), bootstraps the indices on first run, seeds the platform,
imports a CSV file, defines AML/Fraud rules, runs the scheduler and shows
the generated alerts.

    python demo.py
"""
from __future__ import annotations

import json
import os

from dyaf import ingest
from dyaf.alerts.repository import AlertRepository
from dyaf.core.database import Database
from dyaf.datasources.base import DataSourceRegistry
from dyaf.datasources.elasticsearch_source import ElasticsearchDataSource
from dyaf.rules.engine import RuleEngine
from dyaf.rules.models import Rule, validate_rule
from dyaf.rules.query_builder import build_rule_query
from dyaf.rules.repository import RuleRepository
from dyaf.scheduler import RuleScheduler
from dyaf.seed import seed


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    dev_es = None
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if not es_url:
        from dyaf.testing.fake_es import FakeElasticsearch
        dev_es = FakeElasticsearch().start()
        es_url = dev_es.url
        print(f"[demo] ELASTICSEARCH_URL not set -> using embedded dev ES at {es_url}")
    try:
        run(es_url)
    finally:
        if dev_es:
            dev_es.stop()


def run(es_url: str) -> None:
    db = Database(":memory:")
    counts = seed(db)

    registry = DataSourceRegistry()
    tx_source = ElasticsearchDataSource(es_url, "transactions",
                                        mappings=ingest.transactions_mappings())
    wallet_source = ElasticsearchDataSource(es_url, "wallets", timestamp_field=None,
                                            mappings=ingest.wallets_mappings())
    registry.register(tx_source)
    registry.register(wallet_source)

    hr("1) First-run index bootstrap (create index with platform mapping)")
    for source in (tx_source, wallet_source):
        print(f"  ensure_index -> {source.ensure_index()}")
        print(f"  ensure_index -> {source.ensure_index()}  (idempotent)")

    hr("2) Seeding platform data into Elasticsearch (bulk, enriched at ingest)")
    result = tx_source.bulk_index(ingest.enrich_transactions(db, db.list_transactions()),
                                  id_field="transaction_id")
    print(f"  transactions indexed: {result['indexed']} (errors: {len(result['errors'])})")
    result = wallet_source.bulk_index(db.list_wallets(), id_field="wallet_id")
    print(f"  wallets indexed:      {result['indexed']} "
          f"(platform tables: {json.dumps(counts)})")

    hr("3) CSV import (template + upload path used by the UI)")
    template = ingest.csv_template("transactions")
    print("  CSV template header:")
    print("    " + template.splitlines()[0])
    csv_content = (
        "transaction_id,sender_wallet_id,receiver_wallet_id,amount,executed_at,"
        "transaction_type_id,reference_number,fee,currency,device_id\n"
        "TX-CSV-01,W-1001,W-2001,9800.00,2026-07-11T09:00:00,3,REF-CSV-01,10,USD,DEV-42\n"
        "TX-CSV-02,W-1001,W-2001,9650.00,2026-07-11T10:30:00,3,REF-CSV-02,10,USD,DEV-42\n"
    )
    rows, errors = ingest.parse_transactions_csv(csv_content)
    for row in rows:
        db.insert_transaction(ingest.transaction_model_from_row(dict(row)))
    upload = tx_source.bulk_index(ingest.enrich_transactions(db, rows),
                                  id_field="transaction_id")
    print(f"  uploaded CSV rows indexed: {upload['indexed']} (errors: {errors})")

    hr("4) Dynamic field discovery from the live index mapping")
    fields = tx_source.get_fields()
    names = [f.name for f in fields]
    print(f"  transactions index exposes {len(fields)} fields")
    print(f"  new CSV column visible automatically: device_id -> {'device_id' in names}")

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
                {"field": "sender_pep_status", "operator": "eq", "value": True},
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
        Rule(
            name="Same Device Structuring (from CSV column)",
            description="3+ near-threshold cash-outs from one device (device_id came from CSV)",
            data_source="transactions", target_entity="Wallet", group_by="device_id",
            time_window={"value": 48, "unit": "hours"},
            conditions={"field": "amount", "operator": "between", "value": [9000, 9999]},
            aggregation={"type": "count"},
            threshold={"operator": "gte", "value": 2},
            risk_score=70, alert_severity="High",
        ),
    ]

    hr("5) Validating and saving rules (versioned)")
    known = tx_source.field_names()
    for rule in rules:
        errs = validate_rule(rule.to_dict(), known_fields=known, datasource_names=registry.names())
        assert not errs, errs
        rules_repo.save(rule)
        print(f"  [OK] {rule.name}  (v{rule.version}, {rule.alert_severity}, risk={rule.risk_score})")

    hr("6) Elasticsearch query preview for 'Structuring Detection'")
    print(json.dumps(build_rule_query(rules[0].to_dict()), indent=2)[:900] + "\n  ...")

    hr("7) Scheduler run — every fetch goes through Elasticsearch _search")
    for r in scheduler.run_pending():
        print(f"  {r.rule_name}: {r.rows_evaluated} rows, {r.groups_evaluated} entities, "
              f"{r.groups_matched} matched")

    hr("8) Generated alerts")
    for a in alerts_repo.list():
        rr = a["rule_result"]
        print(f"  {a['alert_id']}  [{a['alert_severity']:<8}] score={a['risk_score']:<3} "
              f"rule='{a['rule_name']}' v{a['rule_version']}")
        print(f"      customer={a['customer']}, wallet={a['wallet_id']}, "
              f"value={rr['aggregation_value']} (threshold {rr['threshold']['operator']} "
              f"{rr['threshold']['value']}), txs={len(a['transaction_ids'])}, "
              f"status={a['investigation_status']}")

    hr("9) Investigation workflow")
    first = alerts_repo.list()[0]
    alerts_repo.update_status(first["alert_id"], "In Review")
    alerts_repo.update_status(first["alert_id"], "Closed - Confirmed")
    print(f"  {first['alert_id']} -> {alerts_repo.get(first['alert_id'])['investigation_status']}")

    print(f"\nDemo complete: {len(alerts_repo.list())} alerts generated "
          f"across {len(rules)} rules — all data read from Elasticsearch.")


if __name__ == "__main__":
    main()
