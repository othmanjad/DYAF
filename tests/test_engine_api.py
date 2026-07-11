"""Engine semantics, SELECT validation, previews, dynamic platform features."""
import io

import pytest

RULE = {
    "name": "Debit exceeds credit",
    "from": "transactions",
    "group_by": ["wallet_id"],
    "select": [
        {"name": "debit_sum", "func": "sum", "field": "amount",
         "filter": {"field": "is_debit", "operator": "eq", "value": True}},
        {"name": "credit_sum", "func": "sum", "field": "amount",
         "filter": {"field": "is_debit", "operator": "eq", "value": False}},
    ],
    "having": "debit_sum > credit_sum",
    "time_window": {"value": 30, "unit": "days"},
    "frequency": {"value": 1, "unit": "days"},
    "risk_score": 65, "severity": "Medium",
}


def test_double_entry_storage(client):
    h = client.get("/api/es/health").json()
    # every logical transaction = 2 documents (debit + credit side)
    assert h["indices"]["transactions"]["docs"] % 2 == 0
    fields = {f["name"] for f in
              client.get("/api/datasources/transactions/fields").json()["fields"]}
    assert {"is_debit", "wallet_id", "counterparty_wallet_id",
            "wallet_owner_name", "wallet_pep_status",
            "transaction_type_en", "wallet_expected_weekly_volume"} <= fields
    # computed field appears with its expression
    f = client.get("/api/datasources/transactions/fields").json()["fields"]
    is_round = next(x for x in f if x["name"] == "is_round")
    assert is_round["computed"] and "1000" in is_round["expression"]


def test_debit_vs_credit_select_rule(client):
    r = client.post("/api/rules/test", json=RULE).json()
    by_wallet = {g["group_key"]["wallet_id"]: g for g in r["group_results"]}
    assert by_wallet["W-1001"]["matched"]       # heavy sender
    assert not by_wallet["W-9002"]["matched"]   # settlement wallet only receives
    g = by_wallet["W-1001"]
    assert g["aggregates"]["debit_sum"] > g["aggregates"]["credit_sum"]


def test_match_mode_per_record(client):
    rule = {"name": "Large tx", "from": "transactions",
            "where": {"logic": "AND", "conditions": [
                {"field": "is_debit", "operator": "eq", "value": True},
                {"field": "amount", "operator": "gt", "value": 50000}]},
            "time_window": {"value": 30, "unit": "days"},
            "frequency": {"value": 1, "unit": "days"},
            "risk_score": 80, "severity": "High"}
    r = client.post("/api/rules/test", json=rule).json()
    assert r["groups_matched"] == r["rows_evaluated"] == 1  # the 75k transfer


def test_validation_errors(client):
    bad = {"name": "", "from": "nope",
           "group_by": ["no_field"],
           "select": [{"name": "x!", "func": "wat"},
                      {"name": "s", "func": "sum"}],
           "having": "undefined_agg > 1 AND (",
           "time_window": {"value": 0, "unit": "years"},
           "frequency": {"value": 1, "unit": "hours"},
           "risk_score": 500, "severity": "Extreme"}
    r = client.post("/api/rules/validate", json=bad).json()
    joined = "\n".join(r["errors"])
    assert not r["valid"]
    assert "name is required" in joined
    assert "unknown datasource" in joined
    assert "time_window" in joined
    assert "risk_score" in joined
    assert "severity" in joined
    assert "valid name" in joined          # x! invalid aggregate name
    assert "unknown function" in joined
    assert "requires a field" in joined    # sum without field
    assert "having" in joined.lower()


def test_having_must_reference_defined_aggregates(client):
    rule = dict(RULE, having="debit_sum > mystery")
    r = client.post("/api/rules/validate", json=rule).json()
    assert not r["valid"]
    assert any("mystery" in e for e in r["errors"])


def test_match_mode_requires_where(client):
    rule = {"name": "x", "from": "transactions",
            "time_window": {"value": 1, "unit": "days"},
            "frequency": {"value": 1, "unit": "hours"},
            "risk_score": 10, "severity": "Low"}
    r = client.post("/api/rules/validate", json=rule).json()
    assert any("match mode" in e for e in r["errors"])


def test_preview_sql_and_es_dsl(client):
    r = client.post("/api/rules/preview", json=RULE).json()
    assert r["valid"], r["errors"]
    sql = r["sql"]
    assert "SELECT" in sql and "FILTER" in sql and "GROUP BY wallet_id" in sql
    assert "HAVING debit_sum > credit_sum" in sql
    es = r["es_query"]
    comp = es["aggs"]["by_entity"]["composite"]["sources"]
    assert comp == [{"wallet_id": {"terms": {"field": "wallet_id"}}}]
    inner = es["aggs"]["by_entity"]["aggs"]
    assert inner["debit_sum"]["aggs"]["m"] == {"sum": {"field": "amount"}}
    sel = inner["having"]["bucket_selector"]
    assert sel["buckets_path"] == {"debit_sum": "debit_sum>m",
                                   "credit_sum": "credit_sum>m"}
    assert sel["script"] == "(params.debit_sum > params.credit_sum)"


def test_preview_includes_runtime_fields_for_computed(client):
    rule = client.get("/api/rules/TMPL-TM03").json()
    r = client.post("/api/rules/preview", json=rule).json()
    rm = r["es_query"]["runtime_mappings"]["is_round"]["script"]["source"]
    assert "doc['amount'].value % 1000.0" in rm


def test_rule_versioning(client):
    created = client.post("/api/rules", json=RULE).json()
    assert created["version"] == 1
    updated = client.put(f"/api/rules/{created['rule_id']}",
                         json=dict(RULE, description="v2")).json()
    assert updated["version"] == 2
    versions = client.get(f"/api/rules/{created['rule_id']}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]


# ----------------------------------------------------------------------
# dynamic platform features
# ----------------------------------------------------------------------

def test_named_lists_crud_and_use(client):
    assert client.post("/api/lists", json={
        "name": "watch_wallets", "values": ["W-1003"]}).status_code == 201
    rule = {"name": "watched", "from": "transactions",
            "where": {"logic": "AND", "conditions": [
                {"field": "wallet_id", "operator": "in", "value": "@watch_wallets"},
                {"field": "is_debit", "operator": "eq", "value": True}]},
            "time_window": {"value": 30, "unit": "days"},
            "frequency": {"value": 1, "unit": "days"},
            "risk_score": 50, "severity": "Medium"}
    r = client.post("/api/rules/test", json=rule).json()
    assert r["rows_evaluated"] > 0
    # unknown list rejected at validation
    bad = dict(rule, where={"field": "wallet_id", "operator": "in", "value": "@nope"})
    v = client.post("/api/rules/validate", json=bad).json()
    assert any("@nope" in e for e in v["errors"])


def test_computed_field_crud_and_validation(client):
    r = client.post("/api/computed-fields", json={
        "datasource": "transactions", "name": "fee_ratio",
        "expression": "fee / amount"})
    assert r.status_code == 201
    fields = {f["name"] for f in
              client.get("/api/datasources/transactions/fields").json()["fields"]}
    assert "fee_ratio" in fields
    bad = client.post("/api/computed-fields", json={
        "datasource": "transactions", "name": "x", "expression": "no_field + )"})
    assert bad.status_code == 422
    bad = client.post("/api/computed-fields", json={
        "datasource": "transactions", "name": "x", "expression": "ghost_col * 2"})
    assert bad.status_code == 422 and "ghost_col" in bad.text


def test_dynamic_datasource_lifecycle(client):
    cfg = {"name": "card_events", "timestamp_field": "event_time",
           "id_field": "event_id",
           "required_fields": ["event_id", "event_time", "wallet_id", "amount"],
           "joins": [{"source_field": "wallet_id", "target": "wallets",
                      "prefix": "holder_"}]}
    r = client.post("/api/datasources", json=cfg)
    assert r.status_code == 201 and r.json()["index_setup"]["created"]

    csv_content = ("event_id,event_time,wallet_id,amount,channel\n"
                   "EV-1,now-should-not-parse,W-1001,10,web\n")
    # use a real timestamp
    csv_content = ("event_id,event_time,wallet_id,amount,channel\n"
                   "EV-1,2026-07-10T10:00:00,W-1001,150,web\n"
                   "EV-2,2026-07-11T10:00:00,W-1001,900,app\n")
    up = client.post("/api/datasources/card_events/upload-csv",
                     files={"file": ("c.csv", io.BytesIO(csv_content.encode()), "text/csv")})
    assert up.status_code == 200 and up.json()["indexed"] == 2

    fields = {f["name"] for f in
              client.get("/api/datasources/card_events/fields").json()["fields"]}
    assert {"channel", "holder_owner_name"} <= fields  # dynamic col + join output

    rule = {"name": "card spend", "from": "card_events",
            "group_by": ["wallet_id"],
            "select": [{"name": "total", "func": "sum", "field": "amount"}],
            "having": "total > 1000",
            "time_window": {"value": 365, "unit": "days"},
            "frequency": {"value": 1, "unit": "days"},
            "risk_score": 40, "severity": "Low"}
    result = client.post("/api/rules/test", json=rule).json()
    assert result["groups_matched"] == 1

    assert client.delete("/api/datasources/card_events").status_code == 200
    assert client.delete("/api/datasources/transactions").status_code == 422


def test_field_value_suggestions(client):
    r = client.get("/api/datasources/transactions/fields/is_debit/values").json()
    assert len(r["values"]) == 2
    r = client.get("/api/datasources/transactions/fields/merchant_category/values").json()
    assert "Gambling" in r["values"]


def test_csv_template_and_wallet_upload(client):
    t = client.get("/api/datasources/transactions/csv-template")
    assert t.headers["content-disposition"].endswith('filename="transactions_template.csv"')
    assert t.text.splitlines()[0].startswith("transaction_id,sender_wallet_id")

    wallet_csv = ("wallet_id,owner_name,wallet_type,expected_weekly_volume\n"
                  "W-7777,New Person,Customer Wallet,1234\n")
    r = client.post("/api/datasources/wallets/upload-csv",
                    files={"file": ("w.csv", io.BytesIO(wallet_csv.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["indexed"] == 1
    assert any(w["wallet_id"] == "W-7777" for w in client.get("/api/wallets").json())


def test_scheduler_and_frequency(client):
    client.post("/api/rules/TMPL-TM01/enable?enabled=true")
    r = client.post("/api/scheduler/run").json()
    assert r["executed"] >= 1
    assert client.post("/api/scheduler/run").json()["executed"] == 0  # not due yet


def test_internal_wallets_screen(client):
    iw = client.get("/api/internal-wallets").json()
    assert any(w["wallet_id"] == "W-9001" for w in iw)
    r = client.post("/api/internal-wallets", json={
        "wallet_id": "W-9002", "name": "FX Settlement", "description": "x"})
    assert r.status_code == 201
    assert client.post("/api/internal-wallets", json={
        "wallet_id": "W-404", "name": "x"}).status_code == 422


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "SELECT" in r.text
