"""Reading bank statements: CAMT.053 (the ISO 20022 XML German and EU banks export) and the semicolon CSV most German
online banking offers. Each transaction gets a stable reference (the bank's own, or a hash of its fields), so
importing an overlapping statement twice adds nothing."""
import csv, hashlib, io, re
from datetime import datetime

from defusedxml import ElementTree  # statements are uploaded files: no entity expansion, no external entities

from .db import now


class BadStatement(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, path: str):
    """Namespace-agnostic path lookup (camt.053.001.02 to .08 use different namespaces)."""
    for part in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == part), None)
    return el


def _text(el, path: str) -> str:
    found = _find(el, path)
    return (found.text or "").strip() if found is not None else ""


def parse_camt(data: bytes) -> list[dict]:
    try:
        root = ElementTree.fromstring(data)
    except Exception as e:  # noqa: BLE001 - defusedxml raises several types for hostile or broken XML
        raise BadStatement(f"Not a readable CAMT file: {e}")
    out = []
    for ntry in root.iter():
        if _local(ntry.tag) != "Ntry":
            continue
        amt = _find(ntry, "Amt")
        cents = round(float(amt.text) * 100) * (1 if _text(ntry, "CdtDbtInd") == "CRDT" else -1)
        tx = _find(ntry, "NtryDtls/TxDtls")
        credit = cents > 0
        party = "Dbtr" if credit else "Cdtr"
        name = _text(tx, f"RltdPties/{party}/Nm") or _text(tx, f"RltdPties/{party}/Pty/Nm")
        iban = _text(tx, f"RltdPties/{party}Acct/Id/IBAN")
        purpose = " ".join(u.text.strip() for u in (tx.iter() if tx is not None else []) if _local(u.tag) == "Ustrd" and u.text)
        booked = _text(ntry, "BookgDt/Dt") or _text(ntry, "BookgDt/DtTm")[:10]
        ref = _text(ntry, "AcctSvcrRef") or _text(tx, "Refs/AcctSvcrRef") or _text(tx, "Refs/EndToEndId")
        out.append({"booked": booked, "amount_cents": cents, "name": name, "iban": iban, "purpose": purpose, "ref": ref})
    if not out:
        raise BadStatement("The file has no booked entries (<Ntry>).")
    return out


def _cents(raw: str) -> int:
    s = raw.strip().replace("€", "").replace(" ", "")
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")  # 1.234,56
    else:
        s = s.replace(",", "")
    return round(float(s) * 100)


def parse_csv(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.search(r"buchung|booking|datum|date", l, re.I) and re.search(r"betrag|amount", l, re.I)), None)
    if start is None:
        raise BadStatement("No header row with a date and an amount column found.")
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=";" if lines[start].count(";") >= lines[start].count(",") else ","))
    pick = lambda r, *names: next((r[k] for k in r if k and any(n in k.lower() for n in names) and r[k]), "")
    out = []
    for r in rows:
        amount = pick(r, "betrag", "amount")
        if not amount:
            continue
        raw_date = pick(r, "buchungstag", "buchungsdatum", "booking date", "datum", "date")
        try:
            booked = datetime.strptime(raw_date.strip(), "%d.%m.%Y").date().isoformat()
        except ValueError:
            booked = raw_date.strip()[:10]
        out.append({"booked": booked, "amount_cents": _cents(amount), "name": pick(r, "zahlungspflichtige", "auftraggeber", "empfänger", "name", "payer"),
                    "iban": pick(r, "iban"), "purpose": pick(r, "verwendungszweck", "purpose", "reference", "description"), "ref": pick(r, "referenz", "reference id")})
    if not out:
        raise BadStatement("No transactions found in the file.")
    return out


def store(db, filename: str, data: bytes, by: str) -> dict:
    fmt = "camt.053" if data.lstrip()[:5] == b"<?xml" or b"<Document" in data[:500] else "csv"
    entries = parse_camt(data) if fmt == "camt.053" else parse_csv(data)
    iid = db.execute("insert into imports (filename, fmt, rows, new, duplicates, at, by) values (?, ?, ?, 0, 0, ?, ?)",
                     (filename[:200], fmt, len(entries), now(), by)).lastrowid
    new = 0
    for e in entries:
        if db.execute("select 1 from transactions where booked = ? and amount_cents = ? and coalesce(iban, '') = ? and purpose = ?",
                      (e["booked"], e["amount_cents"], e["iban"][:40], e["purpose"][:500])).fetchone():
            continue  # the same booking from another export of the same account (a CSV overlapping a CAMT file)
        ref = e["ref"] or hashlib.sha256(f"{e['booked']}|{e['amount_cents']}|{e['iban']}|{e['purpose']}".encode()).hexdigest()[:24]
        new += db.execute("""insert or ignore into transactions (ref, booked, amount_cents, name, iban, purpose, import_id, status)
                             values (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (ref, e["booked"], e["amount_cents"], e["name"][:200], e["iban"][:40], e["purpose"][:500], iid,
                           "new" if e["amount_cents"] > 0 else "outgoing")).rowcount
    db.execute("update imports set new = ?, duplicates = ? where id = ?", (new, len(entries) - new, iid))
    return {"id": iid, "format": fmt, "rows": len(entries), "new": new, "duplicates": len(entries) - new}
