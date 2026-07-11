"""REST API + Rule Builder UI host.

Endpoints cover the full requirement set:
* dynamic field discovery per datasource (§9)
* rule CRUD with versioning, validation, query preview, test-before-save (§6, §8)
* rule execution + scheduler trigger (§6)
* alerts listing + investigation status workflow (§7)
* internal wallet settings screen backing API (platform overview)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..alerts.repository import AlertRepository
from ..core.database import Database
from ..core.models import InternalWalletConfig
from ..datasources.base import DataSourceRegistry
from ..datasources.sqlite_source import TransactionsDataSource, WalletsDataSource
from ..rules import aggregations, conditions
from ..rules.engine import RuleEngine
from ..rules.models import AlertSeverity, Rule, TargetEntity, validate_rule
from ..rules.query_builder import build_rule_query
from ..rules.repository import RuleRepository
from ..scheduler import RuleScheduler
from ..seed import seed

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class InternalWalletBody(BaseModel):
    wallet_id: str
    name: str
    description: str = ""


class StatusBody(BaseModel):
    investigation_status: str


def create_app(db_path: str = ":memory:", seed_data: bool = True) -> FastAPI:
    db = Database(db_path)
    if seed_data and not db.query("SELECT 1 FROM wallets LIMIT 1"):
        seed(db)

    registry = DataSourceRegistry()
    registry.register(TransactionsDataSource(db))
    registry.register(WalletsDataSource(db))

    rules_repo = RuleRepository(db)
    alerts_repo = AlertRepository(db)
    engine = RuleEngine(registry, db, alerts_repo)
    scheduler = RuleScheduler(engine, rules_repo)

    app = FastAPI(title="DYAF — AML & Fraud Detection Platform", version="1.0.0")
    # expose for tests
    app.state.db = db
    app.state.registry = registry
    app.state.engine = engine
    app.state.scheduler = scheduler

    # ------------------------------------------------------------------
    # Rule Builder UI
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # ------------------------------------------------------------------
    # Metadata for the Rule Builder (all dynamic — nothing hardcoded)
    # ------------------------------------------------------------------
    @app.get("/api/metadata")
    def metadata():
        return {
            "datasources": registry.names(),
            "operators": conditions.supported_operators(),
            "aggregations": aggregations.available(),
            "target_entities": [e.value for e in TargetEntity],
            "severities": [s.value for s in AlertSeverity],
            "time_units": ["minutes", "hours", "days", "weeks"],
        }

    @app.get("/api/datasources/{name}/fields")
    def datasource_fields(name: str):
        try:
            source = registry.get(name)
        except KeyError:
            raise HTTPException(404, f"Unknown datasource '{name}'")
        return {"datasource": name, "timestamp_field": source.timestamp_field,
                "fields": [f.to_dict() for f in source.get_fields()]}

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------
    def _known_fields(ds_name: str) -> Optional[set]:
        try:
            return registry.get(ds_name).field_names()
        except KeyError:
            return None

    def _validate(defn: dict) -> list[str]:
        return validate_rule(defn, known_fields=_known_fields(defn.get("data_source", "")),
                             datasource_names=registry.names())

    @app.get("/api/rules")
    def list_rules():
        return [r.to_dict() for r in rules_repo.list()]

    @app.get("/api/rules/{rule_id}")
    def get_rule(rule_id: str):
        rule = rules_repo.get(rule_id)
        if not rule:
            raise HTTPException(404, "Rule not found")
        return rule.to_dict()

    @app.get("/api/rules/{rule_id}/versions")
    def rule_versions(rule_id: str):
        return rules_repo.versions(rule_id)

    @app.post("/api/rules/validate")
    def validate(defn: dict):
        errors = _validate(defn)
        return {"valid": not errors, "errors": errors}

    @app.post("/api/rules/preview")
    def preview(defn: dict):
        """Elasticsearch DSL preview of the rule query."""
        errors = _validate(defn)
        if errors:
            return {"valid": False, "errors": errors, "query": None}
        ts = registry.get(defn["data_source"]).timestamp_field
        return {"valid": True, "errors": [], "query": build_rule_query(defn, timestamp_field=ts)}

    @app.post("/api/rules/test")
    def test_rule(defn: dict):
        """Dry-run a rule before saving — no alerts are persisted."""
        errors = _validate(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        rule = Rule.from_dict(defn)
        result = engine.execute(rule, dry_run=True)
        return result.to_dict()

    @app.post("/api/rules", status_code=201)
    def create_rule(defn: dict):
        errors = _validate(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        defn.pop("version", None)
        rule = Rule.from_dict(defn)
        return rules_repo.save(rule).to_dict()

    @app.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, defn: dict):
        if not rules_repo.get(rule_id):
            raise HTTPException(404, "Rule not found")
        errors = _validate(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        defn["rule_id"] = rule_id
        defn.pop("version", None)
        rule = Rule.from_dict(defn)
        return rules_repo.save(rule).to_dict()

    @app.post("/api/rules/{rule_id}/enable")
    def enable_rule(rule_id: str, enabled: bool = True):
        rule = rules_repo.set_enabled(rule_id, enabled)
        if not rule:
            raise HTTPException(404, "Rule not found")
        return rule.to_dict()

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str):
        if not rules_repo.delete(rule_id):
            raise HTTPException(404, "Rule not found")
        return {"deleted": rule_id}

    @app.post("/api/rules/{rule_id}/execute")
    def execute_rule(rule_id: str):
        rule = rules_repo.get(rule_id)
        if not rule:
            raise HTTPException(404, "Rule not found")
        return engine.execute(rule).to_dict()

    @app.post("/api/scheduler/run")
    def run_scheduler():
        results = scheduler.run_pending()
        return {"executed": len(results), "results": [r.to_dict() for r in results]}

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    @app.get("/api/alerts")
    def list_alerts(rule_id: Optional[str] = None, status: Optional[str] = None):
        return alerts_repo.list(rule_id=rule_id, status=status)

    @app.get("/api/alerts/{alert_id}")
    def get_alert(alert_id: str):
        alert = alerts_repo.get(alert_id)
        if not alert:
            raise HTTPException(404, "Alert not found")
        return alert

    @app.put("/api/alerts/{alert_id}/status")
    def update_alert_status(alert_id: str, body: StatusBody):
        try:
            ok = alerts_repo.update_status(alert_id, body.investigation_status)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not ok:
            raise HTTPException(404, "Alert not found")
        return alerts_repo.get(alert_id)

    # ------------------------------------------------------------------
    # Platform data (read) + internal wallet settings (admin screen)
    # ------------------------------------------------------------------
    @app.get("/api/wallets")
    def wallets():
        return db.list_wallets()

    @app.get("/api/transaction-types")
    def transaction_types():
        return db.list_transaction_types()

    @app.get("/api/transactions")
    def transactions(limit: int = 100):
        return db.list_transactions()[-limit:]

    @app.get("/api/internal-wallets")
    def internal_wallets():
        return db.list_internal_wallets()

    @app.post("/api/internal-wallets", status_code=201)
    def upsert_internal_wallet(body: InternalWalletBody):
        if not db.get_wallet(body.wallet_id):
            raise HTTPException(422, f"Wallet '{body.wallet_id}' does not exist")
        db.upsert_internal_wallet(InternalWalletConfig(body.wallet_id, body.name, body.description))
        return {"wallet_id": body.wallet_id, "name": body.name, "description": body.description}

    @app.delete("/api/internal-wallets/{wallet_id}")
    def delete_internal_wallet(wallet_id: str):
        db.delete_internal_wallet(wallet_id)
        return {"deleted": wallet_id}

    return app


app = None  # created lazily by `python -m dyaf.api.app` / uvicorn factory


def main():  # pragma: no cover
    import uvicorn
    uvicorn.run(create_app(db_path="dyaf.db"), host="127.0.0.1",
                port=int(os.environ.get("PORT", 8000)))


if __name__ == "__main__":  # pragma: no cover
    main()
