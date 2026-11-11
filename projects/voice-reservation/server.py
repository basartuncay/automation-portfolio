"""Booking server: public website + guest API, plus agent tools and the owner panel.

GET  /                  the website (site/index.html)
GET  /panel             owner panel, HTTP Basic (password = TOOL_TOKEN)
POST /api/slots         {date, party} -> free times (public)
POST /api/book          {name, phone, date, time, party, notes} -> booking (public, rate limited)
POST /api/cancel        {id, token} -> cancel with the secret from the confirmation (public)
POST /tool/<name>       agent tools, header `Authorization: Bearer $TOOL_TOKEN`
Tool and API errors return 200 with {"error": "<German text>"} so callers can show or say the reason.
"""
import base64
import hmac
import html
import json
import os
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from reservations import TOOLS, Err, cancel_by_token, connect, create_reservation, day_slots

DB = connect(os.environ.get("RES_DB", "reservations.db"))
TOKEN = os.environ.get("TOOL_TOKEN", "")
SITE = Path(__file__).parent / "site" / "index.html"
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")

# shortcut: in-memory limits per client address; behind a proxy every guest shares one address,
# so these act as global caps. Upgrade to X-Forwarded-For from a trusted proxy when deployed.
HITS = defaultdict(deque)


def allowed(key, limit, window):
    q, now = HITS[key], time.time()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def panel():
    """Owner view: upcoming bookings + callback queue. Read-only."""
    rows = DB.execute("SELECT id,name,phone,start,party,notes,status FROM res "
                      "WHERE start>=date('now','-1 day') ORDER BY start").fetchall()
    DB.execute("CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, kind TEXT, phone TEXT, text TEXT, sent INTEGER DEFAULT 0)")
    todo = DB.execute("SELECT kind,phone,text FROM outbox WHERE kind='callback' AND sent=0").fetchall()
    e = html.escape
    res = "".join(f"<tr class='{r['status']}'><td>{e(r['start'][5:10])}</td><td>{e(r['start'][11:16])}</td>"
                  f"<td>{r['party']}</td><td>{e(r['name'])}</td><td>{e(r['phone'])}</td><td>{e(r['notes'] or '')}</td>"
                  f"<td>{'abgesagt' if r['status'] != 'ok' else ''}</td></tr>" for r in rows) \
        or "<tr><td colspan=7>Keine Reservierungen</td></tr>"
    cb = "".join(f"<li><b>{e(t['phone'])}</b> {e(t['text'])}</li>" for t in todo) or "<li>Keine Rückrufe</li>"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Reservierungen</title><style>
:root{{--bg:#fff;--fg:#1b1b1b;--mut:#666;--line:#e3e3e3}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16161a;--fg:#eee;--mut:#9a9aa3;--line:#2c2c33}}}}
body{{background:var(--bg);color:var(--fg);font:16px system-ui;margin:0 auto;max-width:760px;padding:16px}}
h1{{font-size:20px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--mut);font-weight:500}}tr.cancelled{{color:var(--mut);text-decoration:line-through}}li{{margin:6px 0}}
.t{{overflow-x:auto}}</style>
<h1>Reservierungen</h1><div class=t><table><tr><th>Datum<th>Zeit<th>Pers.<th>Name<th>Telefon<th>Notiz<th></tr>{res}</table></div>
<h1>Rückrufe</h1><ul>{cb}</ul>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)

    def _owner(self):
        # HTTP Basic: password = TOOL_TOKEN, so the token never sits in a URL
        try:
            pw = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode().split(":", 1)[1]
        except Exception:
            return False
        return bool(TOKEN) and hmac.compare_digest(pw, TOKEN)

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            return self._send(200, SITE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/panel":
            if self._owner():
                return self._send(200, panel().encode(), "text/html; charset=utf-8")
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="panel"')
            self.end_headers()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 4096:
            return self._send(413, {"error": "too large"})
        try:
            args = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "bad json"})
        ip = self.client_address[0]
        try:
            if self.path.startswith("/tool/"):
                bearer = self.headers.get("Authorization", "")
                if not TOKEN or not hmac.compare_digest(bearer, f"Bearer {TOKEN}"):
                    return self._send(401, {"error": "unauthorized"})
                fn = TOOLS.get(self.path.removeprefix("/tool/"))
                return self._send(200, fn(DB, **args)) if fn else self._send(404, {"error": "unknown tool"})
            if self.path == "/api/slots":
                if not allowed(("slots", ip), 60, 60):
                    return self._send(429, {"error": "Zu viele Anfragen, bitte kurz warten"})
                return self._send(200, {"slots": day_slots(DB, str(args.get("date", "")), int(args.get("party", 0)))})
            if self.path == "/api/book":
                if args.get("website"):  # honeypot field: only bots fill it
                    return self._send(200, {"ok": True})
                if not allowed(("book", ip), 5, 3600):
                    return self._send(429, {"error": "Zu viele Reservierungen, bitte rufen Sie uns an"})
                b = create_reservation(DB, str(args.get("name", "")), str(args.get("phone", "")), str(args.get("date", "")),
                                       str(args.get("time", "")), int(args.get("party", 0)), str(args.get("notes", "")))
                return self._send(200, b)
            if self.path == "/api/cancel":
                if not allowed(("cancel", ip), 20, 3600):
                    return self._send(429, {"error": "Zu viele Anfragen"})
                return self._send(200, cancel_by_token(DB, int(args.get("id", 0)), str(args.get("token", ""))))
            self._send(404, {"error": "not found"})
        except Err as e:
            self._send(200, {"error": str(e)})
        except (TypeError, ValueError):
            self._send(400, {"error": "bad arguments"})


if __name__ == "__main__":
    # empty TOOL_TOKEN refuses panel and tools (the public site and API still work)
    # shortcut: single-threaded server (one sqlite connection); fine for one restaurant, upgrade past ~10 calls/s
    HTTPServer(("127.0.0.1", 8787), H).serve_forever()
