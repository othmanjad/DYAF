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


BUILTIN_DATASOURCES = [
    {"name": "transactions", "es_index": "transactions",
     "timestamp_field": "executed_at", "id_field": "transaction_id",
     "required_fields": ["transaction_id", "sender_wallet_id", "receiver_wallet_id",
                         "amount", "executed_at", "transaction_type_id"],
     "enrichments": ingest.DEFAULT_TRANSACTION_ENRICHMENTS, "builtin": True},
    {"name": "wallets", "es_index": "wallets",
     "timestamp_field": None, "id_field": "wallet_id",
     "required_fields": ["wallet_id", "owner_name"],
     "enrichments": [], "builtin": True},
    {"name": "wallet_transactions", "es_index": "wallet_transactions",
     "timestamp_field": "executed_at", "id_field": "doc_id",
     "required_fields": [], "enrichments": [], "builtin": True},
]

_BUILTIN_MAPPINGS = {
    "transactions": ingest.transactions_mappings,
    "wallets": ingest.wallets_mappings,
    "wallet_transactions": ingest.wallet_transactions_mappings,
}


def _dynamic_mappings(timestamp_field: Optional[str]) -> dict:
    props = {timestamp_field: {"type": "date"}} if timestamp_field else {}
    return {
        "dynamic": True,
        "dynamic_templates": [
            {"strings_as_keywords": {
                "match_mapping_type": "string", "mapping": {"type": "keyword"}}},
        ],
        "properties": props,
    }


def source_from_config(cfg: dict, es_url: str, auth: dict) -> ElasticsearchDataSource:
    mappings_fn = _BUILTIN_MAPPINGS.get(cfg["name"])
    mappings = mappings_fn() if mappings_fn else _dynamic_mappings(cfg.get("timestamp_field"))
    return ElasticsearchDataSource(
        es_url, index=cfg["es_index"], name=cfg["name"],
        timestamp_field=cfg.get("timestamp_field") or None,
        mappings=mappings, **auth)


def build_registry(db: Database, es_url: str, **auth) -> DataSourceRegistry:
    if not db.list_datasource_configs():
        for cfg in BUILTIN_DATASOURCES:
            db.upsert_datasource_config(cfg)
    registry = DataSourceRegistry()
    for cfg in db.list_datasource_configs():
        registry.register(source_from_config(cfg, es_url, auth))
    return registry


def index_platform_data(db: Database, registry: DataSourceRegistry) -> dict:
    """Push the operational store's wallets + enriched transactions to ES."""
    tx_rows = ingest.enrich_transactions(db, db.list_transactions())
    tx_result = registry.get("transactions").bulk_index(tx_rows, id_field="transaction_id")
    wt_result = registry.get("wallet_transactions").bulk_index(
        ingest.explode_wallet_transactions(tx_rows), id_field="doc_id")
    w_result = registry.get("wallets").bulk_index(db.list_wallets(), id_field="wallet_id")
    return {"transactions": tx_result, "wallet_transactions": wt_result,
            "wallets": w_result}


def create_app(db_path: str = ":memory:", es_url: Optional[str] = None,
               seed_data: bool = True, es_auth: Optional[dict] = None) -> FastAPI:
    env_cfg = es_config_from_env()
    es_url = es_url or env_cfg["es_url"]
    auth = es_auth if es_auth is not None else {
        k: v for k, v in env_cfg.items() if k != "es_url"}
    db = Database(db_path)
    registry = build_registry(db, es_url, **auth)

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
    # Datasource configuration (dynamic sources — no code changes needed)
    # ------------------------------------------------------------------
    @app.get("/api/datasource-configs")
    def list_datasource_configs():
        return db.list_datasource_configs()

    @app.post("/api/datasource-configs", status_code=201)
    def create_datasource_config(cfg: dict):
        name = (cfg.get("name") or "").strip()
        es_index = (cfg.get("es_index") or name).strip()
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(422, "name is required (letters, digits, - or _)")
        existing = db.get_datasource_config(name)
        if existing and existing["builtin"]:
            raise HTTPException(422, f"'{name}' is a built-in datasource")
        for e in cfg.get("enrichments") or []:
            if e.get("lookup") not in ingest.LOOKUPS:
                raise HTTPException(422, f"Unknown enrichment lookup '{e.get('lookup')}'"
                                         f" (available: {sorted(ingest.LOOKUPS)})")
            if not e.get("key_field"):
                raise HTTPException(422, "Each enrichment needs a key_field")
        record = {
            "name": name, "es_index": es_index,
            "timestamp_field": (cfg.get("timestamp_field") or "").strip() or None,
            "id_field": (cfg.get("id_field") or "").strip() or None,
            "required_fields": cfg.get("required_fields") or [],
            "enrichments": cfg.get("enrichments") or [],
            "builtin": False,
        }
        db.upsert_datasource_config(record)
        source = source_from_config(record, es_url, auth)
        registry.register(source)
        created = source.ensure_index() if source.ping() else {"created": False,
                                                               "warning": "ES unreachable"}
        return {**record, "index_setup": created}

    @app.delete("/api/datasource-configs/{name}")
    def delete_datasource_config(name: str):
        cfg = db.get_datasource_config(name)
        if not cfg:
            raise HTTPException(404, "Datasource not found")
        if cfg["builtin"]:
            raise HTTPException(422, "Built-in datasources cannot be deleted")
        db.delete_datasource_config(name)
        registry.unregister(name)
        return {"deleted": name, "note": "the Elasticsearch index itself was kept"}

    @app.get("/api/datasources/{name}/fields/{field}/values")
    def field_values(name: str, field: str, size: int = 50):
        source = get_source(name)
        try:
            values = source.field_values(field, size=size)
        except ElasticsearchError as e:
            raise HTTPException(503, str(e))
        except requests.RequestException:
            raise HTTPException(503, f"Elasticsearch is not reachable at {es_url}")
        return {"field": field, "values": values}

    # ------------------------------------------------------------------
    # CSV template + upload
    # ------------------------------------------------------------------
    @app.get("/api/datasources/{name}/csv-template")
    def csv_template(name: str):
        source = get_source(name)
        try:
            content = ingest.csv_template(name)
        except ValueError:
            # dynamic datasource: derive the template from its config + live mapping
            cfg = db.get_datasource_config(name)
            if not cfg or name == "wallet_transactions":
                raise HTTPException(404, f"No CSV template for datasource '{name}'")
            cols = list(cfg["required_fields"])
            if source.ping() and source.index_exists():
                meta = {"doc_id"}
                for fld in source.get_fields():
                    if fld.name not in cols and fld.name not in meta:
                        cols.append(fld.name)
            if cfg.get("id_field") and cfg["id_field"] not in cols:
                cols.insert(0, cfg["id_field"])
            if cfg.get("timestamp_field") and cfg["timestamp_field"] not in cols:
                cols.append(cfg["timestamp_field"])
            if not cols:
                cols = ["id", "value"]
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            _csv.writer(buf).writerow(cols)
            _csv.writer(buf).writerow(["" for _ in cols])
            content = buf.getvalue()
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

        cfg = db.get_datasource_config(name)
        if name == "transactions":
            rows, errors = ingest.parse_transactions_csv(content)
            # keep the operational store consistent, then index enriched docs
            for row in rows:
                db.insert_transaction(ingest.transaction_model_from_row(dict(row)))
            docs = ingest.enrich_transactions(db, rows)
            # keep the per-direction view in sync
            wt = registry.get("wallet_transactions")
            wt.ensure_index()
            wt.bulk_index(ingest.explode_wallet_transactions(docs), id_field="doc_id")
            id_field = "transaction_id"
        elif name == "wallets":
            rows, errors = ingest.parse_wallets_csv(content)
            for row in rows:
                db.upsert_wallet(ingest.wallet_model_from_row(row))
            docs = rows
            id_field = "wallet_id"
        elif name == "wallet_transactions":
            raise HTTPException(422, "wallet_transactions is derived automatically "
                                     "from transactions uploads")
        elif cfg is not None:
            # dynamic datasource: any column structure, config-driven ingest
            rows, errors = ingest.parse_csv(content, tuple(cfg["required_fields"]))
            docs = ingest.apply_enrichments(db, rows, cfg["enrichments"])
            id_field = cfg.get("id_field")
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
            "enrichment_lookups": sorted(ingest.LOOKUPS),
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
        uvicorn.run(create_app(db_path=os.environ.get("DYAF_DB", "dyaf.db"),
                               es_url=os.environ.get("ELASTICSEARCH_URL")),
                    host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", 8000)))
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
