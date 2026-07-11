"""REST API + Rule Builder UI host.

The detection layer always reads from Elasticsearch (ELASTICSEARCH_URL,
default http://localhost:9200). On startup the platform bootstraps the
indices (creates them with the platform mapping on first run) and seeds
demo data when the transactions index is empty.

Endpoints cover the full requirement set:
* Elasticsearch health / first-run index setup
* CSV template download + CSV upload into the indices
* dynamic field discovery per datasource (§9)
* rule CRUD with versioning, validation, query preview, test-before-save (§6, §8)
* rule execution + scheduler trigger (§6)
* alerts listing + investigation status workflow (§7)
* internal wallet settings screen backing API (platform overview)
"""
from __future__ import annotations

import os
from typing import Optional

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..alerts.repository import AlertRepository
from ..core.database import Database
from ..core.models import InternalWalletConfig
from ..datasources.base import DataSourceRegistry
from ..datasources.elasticsearch_source import ElasticsearchDataSource, ElasticsearchError
from .. import ingest
from ..rules import aggregations, conditions
from ..rules.engine import RuleEngine
from ..rules.models import AlertSeverity, Rule, TargetEntity, validate_rule
from ..rules.query_builder import build_rule_query
from ..rules.repository import RuleRepository
from ..scheduler import RuleScheduler
from ..seed import seed

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no override)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def es_config_from_env() -> dict:
    """Elasticsearch connection settings (see .env.example / README)."""
    return {
        "es_url": os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"),
        "username": os.environ.get("ELASTICSEARCH_USERNAME"),
        "password": os.environ.get("ELASTICSEARCH_PASSWORD"),
        "api_key": os.environ.get("ELASTICSEARCH_API_KEY"),
        "ca_cert": os.environ.get("ELASTICSEARCH_CA_CERT"),
        "verify_certs": os.environ.get("ELASTICSEARCH_VERIFY_CERTS", "true").lower()
                        not in ("0", "false", "no"),
    }


class InternalWalletBody(BaseModel):
    wallet_id: str
    name: str
    description: str = ""


class StatusBody(BaseModel):
    investigation_status: str


def build_registry(es_url: str, **auth) -> DataSourceRegistry:
    registry = DataSourceRegistry()
    registry.register(ElasticsearchDataSource(
        es_url, index="transactions", timestamp_field="executed_at",
        mappings=ingest.transactions_mappings(), **auth))
    registry.register(ElasticsearchDataSource(
        es_url, index="wallets", timestamp_field=None,
        mappings=ingest.wallets_mappings(), **auth))
    return registry


def index_platform_data(db: Database, registry: DataSourceRegistry) -> dict:
    """Push the operational store's wallets + enriched transactions to ES."""
    tx_source = registry.get("transactions")
    wallet_source = registry.get("wallets")
    tx_rows = ingest.enrich_transactions(db, db.list_transactions())
    tx_result = tx_source.bulk_index(tx_rows, id_field="transaction_id")
    w_result = wallet_source.bulk_index(db.list_wallets(), id_field="wallet_id")
    return {"transactions": tx_result, "wallets": w_result}


def create_app(db_path: str = ":memory:", es_url: Optional[str] = None,
               seed_data: bool = True, es_auth: Optional[dict] = None) -> FastAPI:
    env_cfg = es_config_from_env()
    es_url = es_url or env_cfg["es_url"]
    auth = es_auth if es_auth is not None else {
        k: v for k, v in env_cfg.items() if k != "es_url"}
    db = Database(db_path)
    registry = build_registry(es_url, **auth)

    rules_repo = RuleRepository(db)
    alerts_repo = AlertRepository(db)
    engine = RuleEngine(registry, db, alerts_repo)
    scheduler = RuleScheduler(engine, rules_repo)

    def setup_indices() -> dict:
        return {name: registry.get(name).ensure_index()
                for name in registry.names()}

    # First-run bootstrap: create indices + seed demo data when empty.
    startup_status: dict = {"es_url": es_url, "reachable": False}
    if registry.get("transactions").ping():
        startup_status["reachable"] = True
        startup_status["indices"] = setup_indices()
        if seed_data:
            if not db.query("SELECT 1 FROM wallets LIMIT 1"):
                seed(db)
            if registry.get("transactions").count() == 0:
                startup_status["seeded"] = index_platform_data(db, registry)

    app = FastAPI(title="DYAF — AML & Fraud Detection Platform", version="2.0.0")
    # expose for tests
    app.state.db = db
    app.state.registry = registry
    app.state.engine = engine
    app.state.scheduler = scheduler
    app.state.startup_status = startup_status

    def get_source(name: str) -> ElasticsearchDataSource:
        try:
            return registry.get(name)
        except KeyError:
            raise HTTPException(404, f"Unknown datasource '{name}'")

    # ------------------------------------------------------------------
    # Rule Builder UI
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # ------------------------------------------------------------------
    # Elasticsearch administration
    # ------------------------------------------------------------------
    @app.get("/api/es/health")
    def es_health():
        tx = registry.get("transactions")
        conn = tx.check_connection()
        info = {"es_url": es_url, "auth_mode": tx.auth_mode,
                "reachable": conn["reachable"],
                "authenticated": conn["authenticated"], "indices": {}}
        if conn.get("error"):
            info["error"] = conn["error"]
        if conn["ok"]:
            for name in registry.names():
                source = registry.get(name)
                exists = source.index_exists()
                info["indices"][name] = {
                    "exists": exists,
                    "docs": source.count() if exists else 0,
                }
        return info

    @app.post("/api/es/setup")
    def es_setup(seed: bool = False):
        """Create missing indices (first-run bootstrap); optionally seed demo data."""
        tx = registry.get("transactions")
        if not tx.ping():
            raise HTTPException(503, f"Elasticsearch is not reachable at {es_url}")
        result = {"indices": setup_indices()}
        if seed and tx.count() == 0:
            from ..seed import seed as seed_fn
            if not db.query("SELECT 1 FROM wallets LIMIT 1"):
                seed_fn(db)
            result["seeded"] = index_platform_data(db, registry)
        return result

    # ------------------------------------------------------------------
    # CSV template + upload
    # ------------------------------------------------------------------
    @app.get("/api/datasources/{name}/csv-template")
    def csv_template(name: str):
        get_source(name)
        try:
            content = ingest.csv_template(name)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return PlainTextResponse(
            content, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}_template.csv"'})

    @app.post("/api/datasources/{name}/upload-csv")
    async def upload_csv(name: str, file: UploadFile = File(...)):
        source = get_source(name)
        if not source.ping():
            raise HTTPException(503, f"Elasticsearch is not reachable at {es_url}")
        try:
            content = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "File must be UTF-8 encoded CSV")

        if name == "transactions":
            rows, errors = ingest.parse_transactions_csv(content)
            # keep the operational store consistent, then index enriched docs
            for row in rows:
                db.insert_transaction(ingest.transaction_model_from_row(dict(row)))
            docs = ingest.enrich_transactions(db, rows)
            id_field = "transaction_id"
        elif name == "wallets":
            rows, errors = ingest.parse_wallets_csv(content)
            for row in rows:
                db.upsert_wallet(ingest.wallet_model_from_row(row))
            docs = rows
            id_field = "wallet_id"
        else:
            raise HTTPException(422, f"CSV upload is not supported for datasource '{name}'")

        if not rows and errors:
            raise HTTPException(422, detail=errors)
        source.ensure_index()
        result = source.bulk_index(docs, id_field=id_field)
        return {"datasource": name, "received_rows": len(rows) + len(errors),
                "indexed": result["indexed"],
                "errors": errors + [str(e) for e in result["errors"]]}

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
            "es_url": es_url,
        }

    @app.get("/api/datasources/{name}/fields")
    def datasource_fields(name: str):
        source = get_source(name)
        try:
            fields = source.get_fields()
        except ElasticsearchError as e:
            raise HTTPException(503, str(e))
        except requests.RequestException:
            raise HTTPException(503, f"Elasticsearch is not reachable at {es_url}")
        return {"datasource": name, "timestamp_field": source.timestamp_field,
                "fields": [f.to_dict() for f in fields]}

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------
    def _known_fields(ds_name: str) -> Optional[set]:
        try:
            return registry.get(ds_name).field_names()
        except Exception:
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


def main():  # pragma: no cover
    import uvicorn
    load_dotenv()  # pick up ELASTICSEARCH_* settings from ./.env if present
    dev_es = None
    if os.environ.get("DYAF_DEV_ES", "").lower() in ("1", "true", "yes") \
            or not os.environ.get("ELASTICSEARCH_URL"):
        # No cluster configured: start the bundled dev/test double so the
        # platform is runnable out of the box. Point ELASTICSEARCH_URL at a
        # real cluster for production use.
        from ..testing.fake_es import FakeElasticsearch
        dev_es = FakeElasticsearch(port=9200 if _port_free(9200) else 0).start()
        os.environ["ELASTICSEARCH_URL"] = dev_es.url
        print(f"[dyaf] ELASTICSEARCH_URL not set -> started embedded dev ES at {dev_es.url}")
    try:
        uvicorn.run(create_app(db_path="dyaf.db", es_url=os.environ.get("ELASTICSEARCH_URL")),
                    host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
    finally:
        if dev_es:
            dev_es.stop()


def _port_free(port: int) -> bool:  # pragma: no cover
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":  # pragma: no cover
    main()
