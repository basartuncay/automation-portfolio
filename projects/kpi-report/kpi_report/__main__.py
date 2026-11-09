import argparse, os, smtplib
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

from .core import build_report, read_orders, render_chart, render_email, sheet_rows


def push_sheet(sheet_id: str, rows: list[list]) -> None:
    import gspread

    ws_name = "Weekly report"
    sh = gspread.service_account().open_by_key(sheet_id)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(ws_name, rows=60, cols=4)
    ws.clear()
    ws.update(rows, "A1")
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.93, "green": 0.95, "blue": 1}})
    ws.format("B2:C5", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}})


def send_email(html_body: str, chart: Path, to: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Weekly KPI report", os.environ["SMTP_FROM"], to
    msg.set_content("Your email client doesn't show HTML. Open the Google Sheet for the report.")
    msg.add_alternative(html_body, subtype="html")
    msg.get_payload()[1].add_related(chart.read_bytes(), "image", "png", cid="<chart>")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the weekly KPI report from an orders CSV")
    ap.add_argument("orders", type=Path)
    ap.add_argument("--week-start", type=date.fromisoformat,
                    help="Monday of the week to report (default: last full week)")
    ap.add_argument("--out", type=Path, default=Path("report"))
    ap.add_argument("--email-to")
    args = ap.parse_args()

    today = date.today()
    start = args.week_start or today - timedelta(days=today.weekday() + 7)
    report = build_report(read_orders(args.orders), start)
    args.out.mkdir(parents=True, exist_ok=True)
    chart = render_chart(report, args.out / "daily.png")
    (args.out / "report.html").write_text(render_email(report, chart_cid=None).replace(
        "<h3>", f"<img src='daily.png' width='560' alt='Daily revenue chart'><h3>", 1))
    if sheet_id := os.environ.get("GOOGLE_SHEET_ID"):
        push_sheet(sheet_id, sheet_rows(report))
    if args.email_to:
        send_email(render_email(report, chart_cid="chart"), chart, args.email_to)
    c = report["current"]
    print(f"week {c['week_start']}: revenue {c['revenue']:,.2f}, {c['orders']} orders, "
          f"anomalies {report['anomalies'] or 'none'} -> {args.out}/report.html")


if __name__ == "__main__":
    main()
