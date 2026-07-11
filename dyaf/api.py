"""FastAPI application: rule authoring (SELECT model), execution, alerts,
data management (datasources / joins / computed fields / lists / CSV) and
the Rule Builder UI.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from . import conditions, dsl, expr, ingest, rules, templates
from .engine import Engine, INVESTIGATION_STATUSES, Scheduler
from .es_client import EsClient, EsError
from .store import Store

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def es_from_env(es_url: Optional[str] = None) -> EsClient:
    return EsClient(
        es_url or os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"),
        username=os.environ.get("ELASTICSEARCH_USERNAME"),
        password=os.environ.get("ELASTICSEARCH_PASSWORD"),
        api_key=os.environ.get("ELASTICSEARCH_API_KEY"),
        ca_cert=os.environ.get("ELASTICSEARCH_CA_CERT"),
        verify_certs=os.environ.get("ELASTICSEARCH_VERIFY_CERTS", "true").lower()
        not in ("0", "false", "no"))


class InternalWalletBody(BaseModel):
    wallet_id: str
    name: str
    description: str = ""


class StatusBody(BaseModel):
    investigation_status: str


def create_app(db_path: str = ":memory:", es_url: Optional[str] = None,
               es: Optional[EsClient] = None, seed: bool = True) -> FastAPI:
    store = Store(db_path)
    es = es or es_from_env(es_url)
    engine = Engine(es, store)
    scheduler = Scheduler(engine, store)

    # ---------------- first-run bootstrap ----------------
    if not store.list_datasources():
        for cfg in ingest.BUILTIN_DATASOURCES:
            store.upsert_datasource(cfg["name"], cfg)
        for cf in templates.DEFAULT_COMPUTED_FIELDS:
            store.upsert_computed_field(cf["datasource"], cf["name"], cf["expression"])
    if seed:
        ingest.seed_reference_data(store)
        if not store.list_rules():
            for tpl in templates.scenario_templates():
                store.save_rule(rules.normalize(tpl))
    if es.ping():
        for cfg in store.list_datasources():
            es.ensure_index(cfg["es_index"], ingest.mappings_for(cfg))
        if seed and es.count("transactions") == 0:
            _seed_indices(store, es)

    app = FastAPI(title="DYAF — AML/Fraud TMS (SELECT engine)", version="2.0.0")
    app.state.store, app.state.es, app.state.engine = store, es, engine

    # ================= helpers =================
    def get_ds(name: str) -> dict:
        cfg = store.get_datasource(name)
        if not cfg:
            raise HTTPException(404, f"Unknown datasource '{name}'")
        return cfg

    def known_fields(cfg: dict) -> Optional[set]:
        try:
            fields = {f["name"] for f in es.get_mapping_fields(cfg["es_index"])}
        except (EsError, requests.RequestException):
            return None
        fields |= {cf["name"] for cf in store.list_computed_fields(cfg["name"])}
        return fields

    def list_resolver(name):
        return store.get_list(name)

    def validate_rule(defn: dict) -> list[str]:
        cfg = store.get_datasource(defn.get("from") or "")
        return rules.validate(
            defn,
            known_fields=known_fields(cfg) if cfg else None,
            known_lists={l["name"] for l in store.list_lists()},
            datasource_names=[d["name"] for d in store.list_datasources()])

    def rule_or_404(rule_id: str) -> dict:
        rule = store.get_rule(rule_id)
        if not rule:
            raise HTTPException(404, "Rule not found")
        return rule

    # ================= UI =================
    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    # ================= metadata =================
    @app.get("/api/metadata")
    def metadata():
        return {
            "datasources": [d["name"] for d in store.list_datasources()],
            "operators": conditions.supported_operators(),
            "agg_funcs": [{"name": k, "needs_field": v}
                          for k, v in rules.AGG_FUNCS.items()],
            "severities": list(rules.SEVERITIES),
            "time_units": list(rules.TIME_UNITS),
            "statuses": list(INVESTIGATION_STATUSES),
            "lists": [l["name"] for l in store.list_lists()],
            "join_targets": list(ingest.JOIN_TARGETS),
            "es_url": es.base_url,
        }

    @app.get("/api/datasources/{name}/fields")
    def fields(name: str):
        cfg = get_ds(name)
        try:
            mapped = es.get_mapping_fields(cfg["es_index"])
        except EsError as e:
            raise HTTPException(503, str(e))
        except requests.RequestException:
            raise HTTPException(503, f"Elasticsearch unreachable at {es.base_url}")
        computed = [{"name": cf["name"], "type": "double", "computed": True,
                     "expression": cf["expression"]}
                    for cf in store.list_computed_fields(name)]
        return {"datasource": name, "timestamp_field": cfg.get("timestamp_field"),
                "fields": mapped + computed}

    @app.get("/api/datasources/{name}/fields/{field}/values")
    def field_values(name: str, field: str, size: int = 50):
        cfg = get_ds(name)
        try:
            return {"field": field, "values": es.field_values(cfg["es_index"], field, size)}
        except (EsError, requests.RequestException) as e:
            raise HTTPException(503, str(e))

    # ================= datasources =================
    @app.get("/api/datasources")
    def list_datasources():
        return store.list_datasources()

    @app.post("/api/datasources", status_code=201)
    def create_datasource(cfg: dict):
        name = (cfg.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(422, "invalid datasource name")
        existing = store.get_datasource(name)
        if existing and existing.get("builtin"):
            raise HTTPException(422, f"'{name}' is built-in")
        for join in cfg.get("joins") or []:
            if join.get("target") not in ingest.JOIN_TARGETS:
                raise HTTPException(422, f"unknown join target '{join.get('target')}' "
                                         f"(available: {list(ingest.JOIN_TARGETS)})")
            if not join.get("source_field"):
                raise HTTPException(422, "join.source_field is required")
        record = {"name": name, "es_index": (cfg.get("es_index") or name).strip(),
                  "timestamp_field": (cfg.get("timestamp_field") or "").strip() or None,
                  "id_field": (cfg.get("id_field") or "").strip() or None,
                  "required_fields": cfg.get("required_fields") or [],
                  "joins": cfg.get("joins") or [], "builtin": False}
        store.upsert_datasource(name, record)
        setup = es.ensure_index(record["es_index"], ingest.mappings_for(record)) \
            if es.ping() else {"created": False, "warning": "ES unreachable"}
        return {**record, "index_setup": setup}

    @app.delete("/api/datasources/{name}")
    def delete_datasource(name: str):
        cfg = get_ds(name)
        if cfg.get("builtin"):
            raise HTTPException(422, "built-in datasources cannot be deleted")
        store.delete_datasource(name)
        return {"deleted": name, "note": "the Elasticsearch index was kept"}

    # ================= computed fields =================
    @app.get("/api/computed-fields")
    def list_computed(datasource: Optional[str] = None):
        return store.list_computed_fields(datasource)

    @app.post("/api/computed-fields", status_code=201)
    def create_computed(body: dict):
        ds, name = body.get("datasource"), (body.get("name") or "").strip()
        expression = (body.get("expression") or "").strip()
        get_ds(ds)
        if not _NAME_RE.match(name):
            raise HTTPException(422, "invalid computed field name")
        try:
            ast = expr.parse(expression)
        except expr.ExprError as e:
            raise HTTPException(422, f"expression: {e}")
        cfg = store.get_datasource(ds)
        kf = known_fields(cfg)
        if kf is not None:
            unknown = expr.variables(ast) - kf - {name}
            if unknown:
                raise HTTPException(422, "expression references unknown field(s): "
                                    + ", ".join(sorted(unknown)))
        store.upsert_computed_field(ds, name, expression)
        return {"datasource": ds, "name": name, "expression": expression}

    @app.delete("/api/computed-fields/{datasource}/{name}")
    def delete_computed(datasource: str, name: str):
        if not store.delete_computed_field(datasource, name):
            raise HTTPException(404, "Computed field not found")
        return {"deleted": name}

    # ================= named lists =================
    @app.get("/api/lists")
    def get_lists():
        return store.list_lists()

    @app.post("/api/lists", status_code=201)
    def create_list(body: dict):
        name = (body.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(422, "invalid list name")
        values = body.get("values")
        if not isinstance(values, list) or not values:
            raise HTTPException(422, "values must be a non-empty array")
        store.upsert_list(name, values, body.get("description", ""))
        return {"name": name, "values": values}

    @app.delete("/api/lists/{name}")
    def delete_list(name: str):
        if not store.delete_list(name):
            raise HTTPException(404, "List not found")
        return {"deleted": name}

    # ================= ES admin / CSV =================
    @app.get("/api/es/health")
    def es_health():
        conn = es.check_connection()
        info = {"es_url": es.base_url, "auth_mode": es.auth_mode, **conn, "indices": {}}
        info.pop("ok", None)
        if conn["ok"]:
            for cfg in store.list_datasources():
                exists = es.index_exists(cfg["es_index"])
                info["indices"][cfg["name"]] = {
                    "exists": exists,
                    "docs": es.count(cfg["es_index"]) if exists else 0}
        return info

    @app.post("/api/es/setup")
    def es_setup(seed_demo: bool = False):
        if not es.ping():
            raise HTTPException(503, f"Elasticsearch unreachable at {es.base_url}")
        result = {"indices": {cfg["name"]: es.ensure_index(cfg["es_index"],
                                                           ingest.mappings_for(cfg))
                              for cfg in store.list_datasources()}}
        if seed_demo and es.count("transactions") == 0:
            result["seeded"] = _seed_indices(store, es)
        return result

    @app.get("/api/datasources/{name}/csv-template")
    def csv_template(name: str):
        cfg = get_ds(name)
        live = None
        if not cfg.get("builtin"):
            try:
                live = es.get_mapping_fields(cfg["es_index"])
            except (EsError, requests.RequestException):
                live = None
        content = ingest.csv_template(cfg, live)
        return PlainTextResponse(content, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{name}_template.csv"'})

    @app.post("/api/datasources/{name}/upload-csv")
    async def upload_csv(name: str, file: UploadFile = File(...)):
        cfg = get_ds(name)
        if not es.ping():
            raise HTTPException(503, f"Elasticsearch unreachable at {es.base_url}")
        try:
            content = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "file must be UTF-8 CSV")
        rows, errors = ingest.parse_csv(content, tuple(cfg.get("required_fields") or []))
        if not rows and errors:
            raise HTTPException(422, detail=errors)

        if name == "wallets":
            for row in rows:
                store.upsert_wallet(row)
            docs = rows
        elif cfg.get("doubleentry"):
            docs = ingest.explode_double_entry(rows)
            docs = ingest.apply_joins(store, docs, cfg.get("joins") or [])
        else:
            docs = ingest.apply_joins(store, rows, cfg.get("joins") or [])
        result = es.bulk_index(cfg["es_index"], docs, id_field=cfg.get("id_field"))
        return {"datasource": name, "received_rows": len(rows) + len(errors),
                "indexed": result["indexed"],
                "documents": len(docs),
                "errors": errors + [str(e) for e in result["errors"]]}

    # ================= rules =================
    @app.get("/api/rules")
    def list_rules():
        return store.list_rules()

    @app.get("/api/rules/{rule_id}")
    def get_rule(rule_id: str):
        return rule_or_404(rule_id)

    @app.get("/api/rules/{rule_id}/versions")
    def rule_versions(rule_id: str):
        return store.rule_versions(rule_id)

    @app.post("/api/rules/validate")
    def validate(defn: dict):
        errors = validate_rule(rules.normalize(defn))
        return {"valid": not errors, "errors": errors}

    @app.post("/api/rules/preview")
    def preview(defn: dict):
        defn = rules.normalize(defn)
        errors = validate_rule(defn)
        if errors:
            return {"valid": False, "errors": errors, "sql": None, "es_query": None}
        cfg = store.get_datasource(defn["from"])
        body = dsl.build(defn, cfg.get("timestamp_field"),
                         computed_fields=store.list_computed_fields(defn["from"]),
                         lists=list_resolver)
        return {"valid": True, "errors": [],
                "sql": rules.to_sql(defn), "es_query": body}

    @app.post("/api/rules/test")
    def test_rule(defn: dict):
        defn = rules.normalize(defn)
        errors = validate_rule(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        return engine.execute(defn, dry_run=True)

    @app.post("/api/rules", status_code=201)
    def create_rule(defn: dict):
        defn = rules.normalize(defn)
        errors = validate_rule(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        defn.pop("version", None)
        return store.save_rule(defn)

    @app.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, defn: dict):
        rule_or_404(rule_id)
        defn = rules.normalize({**defn, "rule_id": rule_id})
        errors = validate_rule(defn)
        if errors:
            raise HTTPException(422, detail=errors)
        defn.pop("version", None)
        return store.save_rule(defn)

    @app.post("/api/rules/{rule_id}/enable")
    def enable_rule(rule_id: str, enabled: bool = True):
        rule = store.set_rule_enabled(rule_id, enabled)
        if not rule:
            raise HTTPException(404, "Rule not found")
        return rule

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str):
        if not store.delete_rule(rule_id):
            raise HTTPException(404, "Rule not found")
        return {"deleted": rule_id}

    @app.post("/api/rules/{rule_id}/execute")
    def execute_rule(rule_id: str):
        return engine.execute(rule_or_404(rule_id))

    @app.post("/api/scheduler/run")
    def run_scheduler():
        results = scheduler.run_pending()
        return {"executed": len(results), "results": results}

    # ================= alerts =================
    @app.get("/api/alerts")
    def list_alerts(rule_id: Optional[str] = None, status: Optional[str] = None):
        return store.list_alerts(rule_id, status)

    @app.get("/api/alerts/{alert_id}")
    def get_alert(alert_id: str):
        alert = store.get_alert(alert_id)
        if not alert:
            raise HTTPException(404, "Alert not found")
        return alert

    @app.put("/api/alerts/{alert_id}/status")
    def set_alert_status(alert_id: str, body: StatusBody):
        if body.investigation_status not in INVESTIGATION_STATUSES:
            raise HTTPException(422, f"status must be one of {INVESTIGATION_STATUSES}")
        if not store.set_alert_status(alert_id, body.investigation_status):
            raise HTTPException(404, "Alert not found")
        return store.get_alert(alert_id)

    # ================= platform reference data =================
    @app.get("/api/wallets")
    def wallets():
        return store.list_wallets()

    @app.get("/api/transaction-types")
    def transaction_types():
        return store.list_transaction_types()

    @app.get("/api/internal-wallets")
    def internal_wallets():
        return store.list_internal_wallets()

    @app.post("/api/internal-wallets", status_code=201)
    def upsert_internal_wallet(body: InternalWalletBody):
        if not store.get_wallet(body.wallet_id):
            raise HTTPException(422, f"wallet '{body.wallet_id}' does not exist")
        store.upsert_internal_wallet(body.wallet_id, body.name, body.description)
        return body.model_dump()

    @app.delete("/api/internal-wallets/{wallet_id}")
    def delete_internal_wallet(wallet_id: str):
        store.delete_internal_wallet(wallet_id)
        return {"deleted": wallet_id}

    return app


def _seed_indices(store: Store, es: EsClient) -> dict:
    tx_cfg = store.get_datasource("transactions")
    docs = ingest.explode_double_entry(ingest.seed_transactions())
    docs = ingest.apply_joins(store, docs, tx_cfg["joins"])
    tx_result = es.bulk_index("transactions", docs, id_field="doc_id")
    w_result = es.bulk_index("wallets", store.list_wallets(), id_field="wallet_id")
    return {"transactions": tx_result, "wallets": w_result}


def main():  # pragma: no cover
    import uvicorn
    load_dotenv()
    dev_es = None
    if not os.environ.get("ELASTICSEARCH_URL"):
        from .fake_es import FakeElasticsearch
        dev_es = FakeElasticsearch().start()
        os.environ["ELASTICSEARCH_URL"] = dev_es.url
        print(f"[dyaf] ELASTICSEARCH_URL not set -> embedded dev ES at {dev_es.url}")
    try:
        uvicorn.run(create_app(db_path=os.environ.get("DYAF_DB", "dyaf.db")),
                    host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", 8000)))
    finally:
        if dev_es:
            dev_es.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
