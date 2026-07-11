"""End-to-end demo: boot the platform, enable the full TM scenario catalog
and run it — every scenario is a SELECT statement, no custom engine code.

    python demo.py
"""
from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from dyaf.api import create_app
from dyaf.fake_es import FakeElasticsearch


def hr(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main():
    dev = None
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if not es_url:
        dev = FakeElasticsearch().start()
        es_url = dev.url
        print(f"[demo] using embedded dev ES at {es_url}")
    try:
        client = TestClient(create_app(db_path=":memory:", es_url=es_url, seed=True))
        run(client)
    finally:
        if dev:
            dev.stop()


def run(client):
    hr("1) Bootstrap: indices created, demo data seeded (double-entry)")
    h = client.get("/api/es/health").json()
    for name, info in h["indices"].items():
        print(f"   {name:14} exists={info['exists']}  docs={info['docs']}")

    hr("2) Scenario catalog (from the AML TMS document) — pure SELECT rules")
    rules = client.get("/api/rules").json()
    for r in rules:
        print(f"   {r['scenario_ref'] or '—':6} {r['name']}")

    hr("3) The SQL behind one rule (TM-02 Pass-Through)")
    preview = client.post("/api/rules/preview",
                          json=client.get("/api/rules/TMPL-TM02").json()).json()
    print(preview["sql"])
    print("\n   -> HAVING compiles to bucket_selector:",
          json.dumps(preview["es_query"]["aggs"]["by_entity"]["aggs"]["having"]))

    hr("4) Enable and execute every scenario")
    total_alerts = 0
    for r in rules:
        client.post(f"/api/rules/{r['rule_id']}/enable?enabled=true")
        result = client.post(f"/api/rules/{r['rule_id']}/execute").json()
        total_alerts += result["groups_matched"]
        print(f"   {r['scenario_ref'] or '—':6} {r['name'][:44]:46} "
              f"rows={result['rows_evaluated']:4}  entities={result['groups_evaluated']:3}  "
              f"alerts={result['groups_matched']}")

    hr("5) Generated alerts (explainable: aggregates + HAVING recorded)")
    for a in client.get("/api/alerts").json():
        aggs = ", ".join(f"{k}={v}" for k, v in a["rule_result"]["aggregates"].items())
        entity = ", ".join(f"{k}={v}" for k, v in a["group_key"].items())
        print(f"   [{a['severity']:8}] {a['scenario_ref'] or '—':6} {a['rule_name'][:38]:40}")
        print(f"       entity: {entity} | customer: {a['customer']}")
        print(f"       {aggs}  ⊨  {a['rule_result']['having']}")

    print(f"\nDemo complete: {total_alerts} alerts across the scenario catalog.")


if __name__ == "__main__":
    main()
