import subprocess, sys
from datetime import date
from pathlib import Path

import pytest

from kpi_report.core import Order, anomalies, build_report, change, read_orders, render_email, sheet_rows, week_metrics

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def orders(tmp_path_factory):
    path = tmp_path_factory.mktemp("d") / "orders.csv"
    subprocess.run([sys.executable, str(ROOT / "make_orders.py"), str(path)], check=True, capture_output=True)
    return read_orders(path)


def o(day, rev, status="paid", product="A", qty=1):
    return Order("x", date.fromisoformat(day), product, qty, rev, status)


def test_week_metrics_excludes_refunds_and_other_weeks():
    m = week_metrics([o("2026-11-02", 100), o("2026-11-03", 50, product="B", qty=2), o("2026-11-04", 30, "refunded"),
                      o("2026-11-09", 999)], date(2026, 11, 2))
    assert (m["revenue"], m["orders"], m["aov"], m["units"], m["refunds"]) == (150, 2, 75.0, 3, 30)
    assert m["refund_rate"] == round(30 / 180, 4)
    assert m["top_products"] == [("A", 100), ("B", 50)]
    assert len(m["daily"]) == 7 and m["daily"]["2026-11-08"] == 0


def test_change_handles_zero_baseline():
    assert change(120, 100) == 0.2 and change(5, 0) is None


def test_anomaly_needs_history_and_flags_spike():
    assert anomalies({"d": 1000}, [100] * 3) == []
    assert anomalies({"d": 1000, "e": 105}, [100, 110, 90, 105, 95, 100, 102, 98]) == ["d"]


def test_sample_data_flags_only_the_promo_day(orders):
    r = build_report(orders, date(2026, 11, 2))
    assert r["anomalies"] == ["2026-11-04"]
    assert r["wow"]["revenue"] is not None and r["current"]["orders"] > 0


def test_sheet_rows_layout(orders):
    rows = sheet_rows(build_report(orders, date(2026, 11, 2)))
    assert rows[0] == ["Metric", "This week", "Last week", "Change"]
    assert rows[1][0] == "Revenue" and rows[1][3].endswith("%")
    assert ["2026-11-04", rows[[r[0] if r else None for r in rows].index("2026-11-04")][1], "anomaly", ""] in rows


def test_email_escapes_product_names_and_shows_alert():
    r = build_report([o("2026-11-02", 10, product="<script>x</script>")], date(2026, 11, 2))
    r["anomalies"] = ["2026-11-02"]
    body = render_email(r, chart_cid="chart")
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "Unusual days: 2026-11-02" in body and "cid:chart" in body
