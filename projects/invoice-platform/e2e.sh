#!/usr/bin/env bash
# End-to-end: start the stack, email 5 messages to the AP inbox, and check what landed in the approval panel.
# Needs Docker and uv. Leaves the stack running unless KEEP=0.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose up -d --build --wait --wait-timeout 300 panel extractor mail
docker compose up -d --build intake

SAMPLES="$(mktemp -d)"
trap 'rm -rf "$SAMPLES"; [ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
(cd ../invoice-extractor && uv run -q python make_samples.py "$SAMPLES" >/dev/null)

python3 - "$SAMPLES" <<'EOF'
import smtplib, sys
from email.message import EmailMessage
from pathlib import Path

d = Path(sys.argv[1])
mails = [
    ("billing@nordicparts.example", "Invoice NP/7781", [d / "nordic_1.pdf"]),
    ("ar@acme.example", "Scanned invoice", [d / "scanned" / "acme_1_scanned.pdf"]),
    ("ar@acme.example", "Invoice 1043", [d / "acme_2_broken.pdf"]),
    ("hello@newvendor.example", "Our receipt", [d / "unknown_vendor.pdf"]),
    ("someone@example.com", "Lunch on Friday?", []),
]
with smtplib.SMTP("127.0.0.1", 3025) as s:
    for sender, subject, files in mails:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, "ap@invoices.example", subject
        m.set_content("Hello,\nplease see attached.")
        for f in files:
            m.add_attachment(f.read_bytes(), maintype="application", subtype="pdf", filename=f.name)
        s.send_message(m)
print(f"sent {len(mails)} emails")
EOF

query() {
  docker compose exec -T panel php -r '
    $db = new PDO("sqlite:/data/database.sqlite");
    $sql = "select v.name vendor, i.number, i.status, i.total, i.problems, i.source_file,
              (select u.email from invoice_events e join users u on u.id = e.user_id
                where e.invoice_id = i.id order by e.id limit 1) created_by
            from invoices i join vendors v on v.id = i.vendor_id where i.source_file like \"email:%\" order by i.id";
    echo json_encode($db->query($sql)->fetchAll(PDO::FETCH_ASSOC));'
}

for _ in $(seq 1 60); do  # the worker polls every 5s
  [ "$(query | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" -ge 4 ] && break
  sleep 2
done

query > "$SAMPLES/rows.json"
python3 - "$SAMPLES/rows.json" <<'EOF'
import json, sys
rows = {r["source_file"]: r for r in json.load(open(sys.argv[1]))}
for r in rows.values():
    print(f"{r['status']:<13} {r['vendor']:<28} {r['number']:<17} {r['total']:>9}  {r['problems'] or ''}")

def check(file, status, **want):
    r = rows.get("email:" + file)
    assert r, f"{file} never reached the panel"
    assert r["status"] == status, (file, r["status"])
    for k, v in want.items():
        ok = v in (r[k] or "") if k == "problems" else r[k] == v
        assert ok, (file, k, r[k])
    assert r["created_by"] == "intake@example.com", (file, r["created_by"])

check("nordic_1.pdf", "pending", vendor="Nordic Parts AB", number="NP/7781", total=6751.25)
check("acme_1_scanned.pdf", "needs_review", number="INV-01042", total=202.78, problems="read by OCR")
check("acme_2_broken.pdf", "needs_review", problems="subtotal + tax != total")
check("unknown_vendor.pdf", "needs_review", vendor="hello@newvendor.example", problems="unknown layout")
assert len(rows) == 4, "the email without an attachment must not create an invoice"
print("PASS: 4 invoices filed (1 ready to approve, 3 in review), 1 email ignored, all attributed to the intake bot")
EOF
docker compose logs intake --no-log-prefix | grep -E "filed|skipping|retried" | tail -8
