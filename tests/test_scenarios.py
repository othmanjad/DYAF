"""Every scenario from the AML TMS catalog (Word document) runs as a pure
SELECT rule — one test per TM template, against the seeded patterns."""
import pytest


def run_template(client, template_id: str) -> dict:
    rule = client.get(f"/api/rules/{template_id}").json()
    result = client.post("/api/rules/test", json=rule)
    assert result.status_code == 200, result.text
    return result.json()


def matched_wallets(result: dict) -> set:
    return {g["group_key"].get("wallet_id") for g in result["group_results"]
            if g["matched"]}


def test_templates_preloaded_and_valid(client):
    rules = {r["rule_id"]: r for r in client.get("/api/rules").json()}
    expected = {f"TMPL-TM{n:02d}" for n in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12)}
    assert expected <= set(rules)
    for rid in expected:
        r = client.post("/api/rules/validate", json=rules[rid]).json()
        assert r["valid"], (rid, r["errors"])
        assert rules[rid]["enabled"] is False  # shipped disabled for tuning


def test_tm01_structuring(client):
    result = run_template(client, "TMPL-TM01")
    assert matched_wallets(result) == {"W-1001"}
    g = next(g for g in result["group_results"] if g["matched"])
    assert g["aggregates"]["cnt"] >= 3 and g["aggregates"]["total"] >= 9000


def test_tm02_pass_through(client):
    result = run_template(client, "TMPL-TM02")
    assert "W-1004" in matched_wallets(result)  # received 30k, forwarded 28k


def test_tm03_round_amounts_via_computed_field(client):
    result = run_template(client, "TMPL-TM03")
    assert "W-1002" in matched_wallets(result)  # 6 x exactly 5,000
    assert "W-1001" not in matched_wallets(result)  # structuring amounts are not round


def test_tm04_high_risk_jurisdictions_via_named_list(client):
    result = run_template(client, "TMPL-TM04")
    assert matched_wallets(result) == {"W-1003"}


def test_tm05_volume_vs_declared_profile(client):
    result = run_template(client, "TMPL-TM05")
    matched = matched_wallets(result)
    assert "W-1001" in matched          # ~114k vs declared 2,000
    assert "W-1002" not in matched      # large but within 3x declared 100,000


def test_tm06_large_single_vs_average(client):
    result = run_template(client, "TMPL-TM06")
    assert "W-1002" in matched_wallets(result)   # 75,000 >= 10k and >= 5x avg
    assert "W-1001" not in matched_wallets(result)  # 9,900 < 10,000 absolute floor


def test_tm07_dormant_reactivation(client):
    result = run_template(client, "TMPL-TM07")
    assert matched_wallets(result) == {"W-1006"}  # only-ever-recent activity
    g = next(g for g in result["group_results"] if g["matched"])
    assert g["aggregates"]["prior"] == 0 and g["aggregates"]["recent"] >= 1


def test_tm09_many_counterparties(client):
    result = run_template(client, "TMPL-TM09")
    assert "W-1005" in matched_wallets(result)   # 12 distinct W-30xx + background
    others = matched_wallets(result) - {"W-1005"}
    assert not others


def test_tm10_new_corridor_composite_group_by(client):
    result = run_template(client, "TMPL-TM10")
    # composite keys: every group is (wallet_id, merchant_country)
    for g in result["group_results"]:
        assert set(g["group_key"]) == {"wallet_id", "merchant_country"}
    # matched corridors are strictly first-time (prior == 0)
    for g in result["group_results"]:
        if g["matched"]:
            assert g["aggregates"]["prior_cnt"] == 0
            assert g["aggregates"]["recent_total"] >= 1000


def test_tm11_gambling_share(client):
    result = run_template(client, "TMPL-TM11")
    assert matched_wallets(result) == {"W-1004"}
    g = next(g for g in result["group_results"] if g["matched"])
    assert g["aggregates"]["gambling"] >= 0.7 * g["aggregates"]["total"]


def test_tm12_pep_high_risk_exposure(client):
    result = run_template(client, "TMPL-TM12")
    assert matched_wallets(result) == {"W-1003"}


def test_full_lifecycle_execute_and_alert(client):
    """Enable TM-01, run it for real, verify the generated alert content."""
    client.post("/api/rules/TMPL-TM01/enable?enabled=true")
    result = client.post("/api/rules/TMPL-TM01/execute").json()
    assert result["groups_matched"] == 1
    alerts = client.get("/api/alerts", params={"rule_id": "TMPL-TM01"}).json()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["wallet_id"] == "W-1001"
    assert a["customer"] == "Ahmad Khalil"
    assert a["scenario_ref"] == "TM-01"
    assert a["rule_result"]["aggregates"]["cnt"] >= 3
    assert a["rule_result"]["having"] == "cnt >= 3 AND total >= 9000"
    assert a["investigation_status"] == "New"

    # duplicate suppression on re-run
    client.post("/api/rules/TMPL-TM01/execute")
    assert len(client.get("/api/alerts", params={"rule_id": "TMPL-TM01"}).json()) == 1

    # investigation workflow
    r = client.put(f"/api/alerts/{a['alert_id']}/status",
                   json={"investigation_status": "Escalated"}).json()
    assert r["investigation_status"] == "Escalated"
