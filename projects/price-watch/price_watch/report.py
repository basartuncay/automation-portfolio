"""Where we stand per product, and the weekly email digest built from it."""
import html, os, smtplib
from email.message import EmailMessage

from .db import money


def positions(conn) -> list[dict]:
    """For each product we sell: our price, every competitor's current price, and whether we're undercut."""
    comps = [r["name"] for r in conn.execute("select name from competitors order by id")]
    out = []
    for o in conn.execute("select * from our_products order by sku"):
        rows = conn.execute("""select c.name, p.price, p.in_stock, p.active from products p join competitors c on c.id = p.competitor_id
                               where p.pid = ?""", (o["pid"],)).fetchall()
        theirs = {r["name"]: r for r in rows if r["active"]}
        live = [r["price"] for r in theirs.values() if r["in_stock"]]
        cheapest = min(live) if live else None
        out.append({"sku": o["sku"], "title": o["title"], "pid": o["pid"], "ours": o["our_price"], "currency": o["currency"],
                    "competitors": {c: theirs.get(c) for c in comps}, "cheapest": cheapest,
                    "gap_pct": None if cheapest is None else 100 * (o["our_price"] - cheapest) / o["our_price"]})
    return out


def build_digest(conn) -> tuple[str, str]:
    pos = positions(conn)
    undercut = sorted((p for p in pos if p["gap_pct"] and p["gap_pct"] > 0), key=lambda p: -p["gap_pct"])
    week = conn.execute("select kind, count(*) n from alerts where at >= datetime('now', '-7 days') group by kind").fetchall()
    health = conn.execute("""select c.name, r.status, r.finished, r.products from competitors c left join runs r on r.id =
                             (select max(id) from runs where competitor_id = c.id) order by c.id""").fetchall()
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(p['title'])}</td><td>{money(p['ours'], p['currency'])}</td><td>{money(p['cheapest'], p['currency'])}</td>"
                   f"<td style='color:#b91c1c'>{p['gap_pct']:.1f}% above</td></tr>" for p in undercut) \
        or "<tr><td colspan=4>You are the cheapest (or tied) on every tracked product.</td></tr>"
    counts = ", ".join(f"{r['n']} {r['kind'].replace('_', ' ')}" for r in week) or "no alerts"
    checks = "".join(f"<li>{esc(h['name'])}: {esc(h['status'] or 'not crawled yet')}"
                     f"{' · ' + str(h['products']) + ' products' if h['products'] else ''} · {esc(h['finished'] or '')}</li>" for h in health)
    subject = f"Price Watch weekly: {len(undercut)} product(s) undercut"
    body = f"""<div style="font-family:system-ui,sans-serif;max-width:640px">
<h2>Price Watch: weekly digest</h2>
<p><b>{len(undercut)}</b> of {len(pos)} tracked products are cheaper at a competitor. This week: {esc(counts)}.</p>
<table cellpadding=6 style="border-collapse:collapse;width:100%"><tr style="text-align:left;background:#f3f4f6">
<th>Product</th><th>Yours</th><th>Cheapest rival</th><th>Gap</th></tr>{rows}</table>
<h3>Crawl health</h3><ul>{checks}</ul></div>"""
    return subject, body


def send_digest(conn, to: str, send=None) -> EmailMessage:
    subject, body = build_digest(conn)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ.get("DIGEST_FROM", "price-watch@example.com"), to, subject
    msg.set_content("This digest is HTML; open it in a mail client that shows HTML.")
    msg.add_alternative(body, subtype="html")
    if send:
        send(msg)
    else:
        e = os.environ
        with smtplib.SMTP(e.get("SMTP_HOST", "localhost"), int(e.get("SMTP_PORT", 587)), timeout=20) as s:
            if e.get("SMTP_STARTTLS", "true").lower() == "true":
                s.starttls()
            if e.get("SMTP_USER"):
                s.login(e["SMTP_USER"], e["SMTP_PASSWORD"])
            s.send_message(msg)
    return msg
