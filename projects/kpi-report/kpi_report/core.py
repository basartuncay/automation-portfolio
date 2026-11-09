"""Weekly KPI report: orders -> metrics with week-over-week change and anomaly flags -> Sheets payload + HTML email."""
import csv, html, statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass
class Order:
    id: str
    day: date
    product: str
    qty: int
    revenue: float
    status: str  # paid | refunded


def read_orders(path: Path) -> list[Order]:
    with open(path, newline="") as f:
        return [Order(r["order_id"], date.fromisoformat(r["date"]), r["product"], int(r["qty"]), float(r["revenue"]),
                      r["status"]) for r in csv.DictReader(f)]


def week_metrics(orders: list[Order], start: date) -> dict:
    end = start + timedelta(days=7)
    week = [o for o in orders if start <= o.day < end]
    paid = [o for o in week if o.status == "paid"]
    revenue = round(sum(o.revenue for o in paid), 2)
    refunds = round(sum(o.revenue for o in week if o.status == "refunded"), 2)
    daily = defaultdict(float)
    for o in paid:
        daily[o.day] += o.revenue
    by_product = Counter()
    for o in paid:
        by_product[o.product] += o.revenue
    return {
        "week_start": start.isoformat(), "revenue": revenue, "orders": len(paid),
        "aov": round(revenue / len(paid), 2) if paid else 0.0,
        "units": sum(o.qty for o in paid), "refunds": refunds,
        "refund_rate": round(refunds / (revenue + refunds), 4) if revenue + refunds else 0.0,
        "daily": {(start + timedelta(days=i)).isoformat(): round(daily[start + timedelta(days=i)], 2) for i in range(7)},
        "top_products": [(p, round(v, 2)) for p, v in by_product.most_common(5)],
    }


def change(now: float, before: float) -> float | None:
    return None if not before else round((now - before) / before, 4)


def anomalies(daily: dict[str, float], history: list[float], z: float = 3.0) -> list[str]:
    """Days whose revenue is more than z standard deviations from the trailing history."""
    if len(history) < 7:
        return []  # shortcut: needs a week of history; with less, nothing is flagged
    mean, sd = statistics.mean(history), statistics.pstdev(history)
    if sd == 0:
        return []
    return [d for d, v in daily.items() if abs(v - mean) / sd > z]


def build_report(orders: list[Order], week_start: date) -> dict:
    cur, prev = week_metrics(orders, week_start), week_metrics(orders, week_start - timedelta(days=7))
    hist_start = week_start - timedelta(days=28)
    history = [sum(o.revenue for o in orders if o.status == "paid" and o.day == hist_start + timedelta(days=i))
               for i in range(28)]
    return {"current": cur, "previous": prev,
            "wow": {k: change(cur[k], prev[k]) for k in ("revenue", "orders", "aov", "units")},
            "anomalies": anomalies(cur["daily"], history)}


def fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1%}"


def sheet_rows(r: dict) -> list[list]:
    c, p, w = r["current"], r["previous"], r["wow"]
    rows = [["Metric", "This week", "Last week", "Change"]]
    for key, label in (("revenue", "Revenue"), ("orders", "Orders"), ("aov", "Avg order value"), ("units", "Units sold")):
        rows.append([label, c[key], p[key], fmt_pct(w[key])])
    rows.append(["Refund rate", f"{c['refund_rate']:.1%}", f"{p['refund_rate']:.1%}", ""])
    rows += [[], ["Day", "Revenue", "Flag", ""]]
    rows += [[d, v, "anomaly" if d in r["anomalies"] else "", ""] for d, v in c["daily"].items()]
    rows += [[], ["Top products", "Revenue", "", ""]] + [[p_, v, "", ""] for p_, v in c["top_products"]]
    return rows


def render_email(r: dict, chart_cid: str | None = None) -> str:
    c, w = r["current"], r["wow"]
    color = lambda v: "#888" if v is None else ("#1a7f37" if v >= 0 else "#cf222e")
    tiles = "".join(
        f"<td style='padding:12px 16px;border:1px solid #ddd'><div style='color:#666;font-size:12px'>{label}</div>"
        f"<div style='font-size:22px;font-weight:600'>{value}</div>"
        f"<div style='color:{color(w[key])};font-size:12px'>{fmt_pct(w[key])} vs last week</div></td>"
        for key, label, value in (("revenue", "Revenue", f"{c['revenue']:,.2f}"), ("orders", "Orders", c["orders"]),
                                  ("aov", "Avg order", f"{c['aov']:,.2f}"), ("units", "Units", c["units"])))
    top = "".join(f"<li>{html.escape(p)}: {v:,.2f}</li>" for p, v in c["top_products"])
    alert = (f"<p style='color:#cf222e'>Unusual days: {', '.join(r['anomalies'])}</p>" if r["anomalies"] else "")
    chart = f"<img src='cid:{chart_cid}' width='560' alt='Daily revenue chart'>" if chart_cid else ""
    return (f"<h2>Weekly report: week of {c['week_start']}</h2><table style='border-collapse:collapse'><tr>{tiles}</tr></table>"
            f"{alert}{chart}<h3>Top products</h3><ol>{top}</ol>")


def render_chart(r: dict, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    days = list(r["current"]["daily"])
    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ax.bar(days, list(r["current"]["daily"].values()), color=["#cf222e" if d in r["anomalies"] else "#2f6fde" for d in days],
           label="This week")
    ax.plot(days, list(r["previous"]["daily"].values()), color="#888", marker="o", label="Last week")
    ax.set_xticks(range(7), [d[5:] for d in days])
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
