from datetime import datetime, timezone

import pytest

from dyaf.alerts.repository import AlertRepository
from dyaf.core.database import Database
from dyaf.datasources.base import DataSourceRegistry
from dyaf.datasources.sqlite_source import TransactionsDataSource, WalletsDataSource
from dyaf.rules.engine import RuleEngine
from dyaf.rules.models import Rule, validate_rule
from dyaf.rules.repository import RuleRepository
from dyaf.scheduler import RuleScheduler
from dyaf.seed import seed

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env():
    db = Database(":memory:")
    seed(db, now=NOW)
    registry = DataSourceRegistry()
    registry.register(TransactionsDataSource(db))
    registry.register(WalletsDataSource(db))
    engine = RuleEngine(registry, db, AlertRepository(db))
    return db, registry, engine


def structuring_rule() -> Rule:
    return Rule(
        name="Structuring Detection",
        data_source="transactions",
        target_entity="Wallet",
        group_by="sender_wallet_id",
        time_window={"value": 24, "unit": "hours"},
        conditions={"logic": "AND", "conditions": [
            {"field": "amount", "operator": "between", "value": [9000, 9999]},
            {"field": "transaction_type_en", "operator": "eq", "value": "Cash Out"},
        ]},
        aggregation={"type": "count"},
        threshold={"operator": "gte", "value": 5},
        risk_score=90, alert_severity="Critical",
    )


def test_structuring_rule_fires_for_expected_wallet(env):
    db, registry, engine = env
    result = engine.execute(structuring_rule(), now=NOW)
    assert result.groups_matched == 1
    alert = result.alerts[0]
    assert alert.wallet_id == "W-1001"
    assert alert.customer == "Ahmad Khalil"
    assert alert.alert_severity == "Critical"
    assert alert.risk_score == 90
    assert alert.rule_result["aggregation_value"] >= 5
    assert len(alert.transaction_ids) > 0
    assert alert.investigation_status == "New"
    # persisted
    saved = AlertRepository(db).list()
    assert any(a["alert_id"] == alert.alert_id for a in saved)


def test_dry_run_does_not_persist_alerts(env):
    db, registry, engine = env
    result = engine.execute(structuring_rule(), now=NOW, dry_run=True)
    assert result.groups_matched == 1
    assert AlertRepository(db).list() == []


def test_duplicate_suppression(env):
    db, registry, engine = env
    rule = structuring_rule()
    engine.execute(rule, now=NOW)
    engine.execute(rule, now=NOW)
    alerts = AlertRepository(db).list(status="New")
    assert len(alerts) == 1


def test_large_transfer_rule(env):
    db, registry, engine = env
    rule = Rule(
        name="Large Single Transfer", data_source="transactions",
        target_entity="Transaction", group_by="transaction_id",
        time_window={"value": 7, "unit": "days"},
        aggregation={"type": "max", "field": "amount"},
        threshold={"operator": "gte", "value": 50000},
        risk_score=80, alert_severity="High",
    )
    result = engine.execute(rule, now=NOW)
    assert result.groups_matched == 1
    assert result.alerts[0].customer == "Layla Hassan"
    assert result.alerts[0].rule_result["aggregation_value"] == 75000


def test_percentage_rule_gambling_spend(env):
    db, registry, engine = env
    rule = Rule(
        name="Gambling Spend Share", data_source="transactions",
        target_entity="Wallet", group_by="sender_wallet_id",
        time_window={"value": 7, "unit": "days"},
        conditions={"field": "transaction_type_en", "operator": "eq", "value": "Card Payment"},
        aggregation={"type": "percentage", "field": "amount",
                     "config": {"numerator_condition":
                                {"field": "merchant_category", "operator": "eq", "value": "Gambling"}}},
        threshold={"operator": "gt", "value": 70},
        risk_score=60, alert_severity="Medium",
    )
    result = engine.execute(rule, now=NOW)
    matched = [g for g in result.group_results if g.matched]
    assert [g.group_key for g in matched] == ["W-1004"]
    assert matched[0].aggregation_value > 70


def test_high_risk_country_rule_uses_denormalized_wallet_fields(env):
    db, registry, engine = env
    rule = Rule(
        name="High-Risk Country Remittances", data_source="transactions",
        target_entity="Customer", group_by="sender_owner_name",
        time_window={"value": 7, "unit": "days"},
        conditions={"logic": "AND", "conditions": [
            {"field": "transaction_type_en", "operator": "eq", "value": "International Remittance"},
            {"field": "merchant_country", "operator": "in", "value": ["IR", "KP", "SY", "MM"]},
            {"field": "sender_pep_status", "operator": "eq", "value": 1},
        ]},
        aggregation={"type": "sum", "field": "amount"},
        threshold={"operator": "gt", "value": 5000},
        risk_score=95, alert_severity="Critical",
    )
    result = engine.execute(rule, now=NOW)
    assert result.groups_matched == 1
    alert = result.alerts[0]
    assert alert.customer == "Omar Nasser"
    assert alert.wallet_id == "W-1003"


def test_time_window_excludes_old_transactions(env):
    db, registry, engine = env
    rule = structuring_rule()
    rule.time_window = {"value": 10, "unit": "minutes"}
    result = engine.execute(rule, now=NOW)
    assert result.groups_matched == 0


def test_rule_validation_against_dynamic_fields(env):
    db, registry, engine = env
    fields = registry.get("transactions").field_names()
    defn = structuring_rule().to_dict()
    assert validate_rule(defn, known_fields=fields, datasource_names=["transactions"]) == []

    defn["group_by"] = "no_such_field"
    defn["aggregation"] = {"type": "sum", "field": "also_missing"}
    defn["threshold"] = {"operator": "wat", "value": "x"}
    errors = "\n".join(validate_rule(defn, known_fields=fields, datasource_names=["transactions"]))
    assert "no_such_field" in errors
    assert "also_missing" in errors
    assert "threshold.operator" in errors


def test_rule_repository_versioning(env):
    db, registry, engine = env
    repo = RuleRepository(db)
    rule = structuring_rule()
    saved = repo.save(rule)
    assert saved.version == 1
    saved.description = "updated"
    saved = repo.save(saved)
    assert saved.version == 2
    versions = repo.versions(saved.rule_id)
    assert [v["version"] for v in versions] == [1, 2]
    assert repo.get(saved.rule_id).version == 2


def test_alert_records_rule_version(env):
    db, registry, engine = env
    repo = RuleRepository(db)
    rule = repo.save(structuring_rule())
    rule = repo.save(rule)  # bump to v2
    result = engine.execute(rule, now=NOW)
    assert result.alerts[0].rule_version == 2


def test_scheduler_respects_frequency(env):
    db, registry, engine = env
    repo = RuleRepository(db)
    rule = structuring_rule()
    rule.execution_frequency = {"value": 1, "unit": "hours"}
    repo.save(rule)
    scheduler = RuleScheduler(engine, repo)

    first = scheduler.run_pending(now=NOW)
    assert len(first) == 1
    # 10 minutes later: not due yet
    from datetime import timedelta
    assert scheduler.run_pending(now=NOW + timedelta(minutes=10)) == []
    # 1 hour later: due again
    assert len(scheduler.run_pending(now=NOW + timedelta(hours=1))) == 1


def test_disabled_rules_are_not_scheduled(env):
    db, registry, engine = env
    repo = RuleRepository(db)
    rule = repo.save(structuring_rule())
    repo.set_enabled(rule.rule_id, False)
    scheduler = RuleScheduler(engine, repo)
    assert scheduler.run_pending(now=NOW) == []
