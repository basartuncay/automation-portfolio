import re

import duckdb
import pytest
from fastapi.testclient import TestClient

from pipeline import run as runner
from pipeline.extract import SchemaDrift, parse_ads_csv
from pipeline.sources import create_app as create_sources
from pipeline.web import chart, create_app as create_web

ADMIN = {"x-admin": "a" * 16}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"DATA_DIR": str(tmp_path), "SOURCES_ADMIN_TOKEN": "a" * 16, "SOURCES_API_TOKEN": "t" * 16,
                 "SOURCES_URL": "http://src", "HTTP_BACKOFF": "0", "HTTP_TRIES": "3"}.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_sources(), base_url="http://src")


def served(sql):
    con = duckdb.connect(str(runner.paths()["serving"]), read_only=True)
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


def totals():
    r = served("select sum(orders), round(sum(gross), 2), round(sum(refunds), 2), round(sum(net), 2), round(sum(ad_spend), 2) from marts.fct_daily")
    return {"orders": r[0], "gross": float(r[1]), "refunds": float(r[2]), "net": float(r[3]), "ad_spend": float(r[4])}


def truth(src):
    t = src.get("/_admin/truth", headers=ADMIN).json()
    return {k: t[k] for k in ("orders", "gross", "refunds", "net", "ad_spend")}


def test_warehouse_matches_the_sources_exactly_and_reruns_are_idempotent(env):
    src = env
    r = runner.run_once("test", http=src)
    assert r["status"] == "success" and r["models"] == 10 and r["tests_passed"] >= 15 and not r["failures"]
    assert totals() == truth(src)
    assert served("select count(distinct customer_id) from marts.dim_customers")[0] == src.get("/_admin/truth", headers=ADMIN).json()["customers"]
    again = runner.run_once("test", http=src)
    assert again["status"] == "success" and again["sources"]["shop orders"]["rows"] > 0  # the overlap window re-reads
    assert totals() == truth(src)  # ...and dedup keeps the numbers exact


def test_late_refund_changes_a_past_day(env):
    src = env
    runner.run_once("test", http=src)
    day = served("select order_date from marts.fct_orders where order_id = 5003")[0]
    before = float(served(f"select net from marts.fct_daily where day = '{day}'")[0])
    total = float(served("select gross from marts.fct_orders where order_id = 5003")[0])
    src.post("/_admin/chaos", json={"late_refund": 5003}, headers=ADMIN)
    assert runner.run_once("test", http=src)["status"] == "success"
    assert round(float(served(f"select net from marts.fct_daily where day = '{day}'")[0]), 2) == round(before - total, 2)
    assert totals() == truth(src)


def test_failed_data_test_is_never_published(env):
    src = env
    runner.run_once("test", http=src)
    good = totals()
    src.post("/_admin/chaos", json={"negative_spend": True}, headers=ADMIN)
    r = runner.run_once("test", http=src)
    assert r["status"] == "failed" and [f["name"] for f in r["failures"]] == ["assert_no_negative_ad_spend"] and r["skipped"] > 0
    assert totals() == good  # the dashboard still serves the last verified build
    src.post("/_admin/chaos", json={"clear_negative_spend": True}, headers=ADMIN)
    assert runner.run_once("test", http=src)["status"] == "success"


def test_renamed_csv_column_stops_the_run_before_dbt(env):
    src = env
    runner.run_once("test", http=src)
    good = totals()
    src.post("/_admin/chaos", json={"renamed_column": True}, headers=ADMIN)
    r = runner.run_once("test", http=src)
    assert r["status"] == "failed" and r["models"] == 0
    assert r["failures"][0]["name"] == "google ads" and "missing column(s) ['Cost']" in r["failures"][0]["message"]
    assert "rows" in r["sources"]["meta ads"]  # other sources still loaded
    assert totals() == good


def test_outages_are_retried_then_reported(env):
    src = env
    src.post("/_admin/chaos", json={"fail": 2}, headers=ADMIN)
    assert runner.run_once("test", http=src)["status"] == "success"  # two 503s, then fine
    src.post("/_admin/chaos", json={"fail": 99}, headers=ADMIN)
    r = runner.run_once("test", http=src)
    assert r["status"] == "failed" and all("still failing after 3 tries (503)" in f["message"] for f in r["failures"])


def test_csv_mapping_and_drift():
    rows = parse_ads_csv("meta", "date_start,campaign_name,amount_spent,clicks,impressions\n2026-09-01,m,12.50,3,100\n")
    assert rows[0]["spend"] == 12.5 and str(rows[0]["day"]) == "2026-09-01"
    with pytest.raises(SchemaDrift, match="missing column"):
        parse_ads_csv("google", "Day,Campaign,Cost (EUR),Clicks,Impr.\n")


def test_chart_scales():
    c = chart([("2026-09-01", 100, 50), ("2026-09-02", 200, 0)])
    assert len(c["bars"]) == 2 and c["bars"][1]["h"] > c["bars"][0]["h"] and c["line"].count(",") == 2


def test_dashboard_banner_follows_the_runs(env, monkeypatch):
    src = env
    monkeypatch.setenv("DEMO_SEED", "true")
    web = TestClient(create_web(secret="s"), follow_redirects=False)
    assert web.get("/").headers["location"] == "/login"
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "finance@example.com", "password": "password", "csrf": token})
    assert "No verified data yet" in web.get("/").text
    runner.run_once("test", http=src)
    page = web.get("/").text
    assert "All data tests passed" in page and "net revenue, last 30 days" in page and "<rect" in page
    src.post("/_admin/chaos", json={"negative_spend": True}, headers=ADMIN)
    runner.run_once("test", http=src)
    page = web.get("/").text
    assert "The latest run failed" in page and "assert_no_negative_ad_spend" in page and "You're seeing data verified at" in page
    assert "fct_daily" in web.get("/model").text
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/runs").text).group(1)
    assert web.post("/runs", data={"csrf": token}).status_code == 303
    assert runner.runs_db().execute("select count(*) from requests where done = 0").fetchone()[0] == 1
