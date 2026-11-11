"""Getting leads in: CSV exports from Apollo, LinkedIn Sales Navigator tools or a spreadsheet, and a signed web-form
webhook. Every row is normalised, checked against the suppression list and merged by email, and where each lead came
from is kept."""
import csv, io, json, re

from .db import event, now

FIELDS = {  # our field -> header names seen in the wild (lowercased, spaces and underscores removed)
    "email": ["email", "emailaddress", "workemail", "e-mail", "mail", "businessemail"],
    "first_name": ["firstname", "first", "givenname", "vorname"],
    "last_name": ["lastname", "last", "surname", "familyname", "nachname"],
    "title": ["title", "jobtitle", "position", "role", "headline"],
    "company": ["company", "companyname", "organization", "organisation", "account", "accountname", "firma"],
    "website": ["website", "companywebsite", "companydomain", "domain", "url", "web"],
    "country": ["country", "companycountry", "personcountry", "land", "location"],
    "linkedin": ["linkedin", "personlinkedinurl", "linkedinurl", "linkedinprofile", "profileurl"],
}
FORMATS = {"apollo": {"personlinkedinurl", "emailstatus"}, "salesnav": {"profileurl", "jobtitle"}}  # for the import log
COUNTRIES = {"germany": "DE", "deutschland": "DE", "de": "DE", "austria": "AT", "österreich": "AT", "at": "AT", "switzerland": "CH",
             "schweiz": "CH", "ch": "CH", "netherlands": "NL", "nl": "NL", "united kingdom": "GB", "uk": "GB", "gb": "GB",
             "united states": "US", "usa": "US", "us": "US", "france": "FR", "fr": "FR", "spain": "ES", "es": "ES", "turkey": "TR", "türkiye": "TR"}
FREE = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "gmx.de", "gmx.net", "web.de",
        "t-online.de", "aol.com", "proton.me", "protonmail.com", "mail.ru", "yandex.com"}
ROLE = {"info", "sales", "hello", "contact", "office", "support", "team", "admin", "kontakt", "service", "marketing", "jobs", "noreply"}
EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")


class BadFile(Exception):
    pass


def key(h: str) -> str:
    return re.sub(r"[\s_\-.]", "", h.strip().lower()).replace("e-mail", "email")


def domain_of(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0].split("?")[0]
    return d.removeprefix("www.")


def country_of(raw: str) -> str:
    raw = (raw or "").strip().lower()
    return COUNTRIES.get(raw) or COUNTRIES.get(raw.split(",")[-1].strip(), raw.upper()[:2] if len(raw) == 2 else "")


def normalize(row: dict) -> dict:
    lead = {f: (row.get(f) or "").strip() for f in FIELDS}
    lead["email"] = lead["email"].lower().strip("<>; ")
    lead["first_name"] = lead["first_name"].title() if lead["first_name"].isupper() or lead["first_name"].islower() else lead["first_name"]
    lead["last_name"] = lead["last_name"].title() if lead["last_name"].isupper() or lead["last_name"].islower() else lead["last_name"]
    lead["country"] = country_of(lead["country"])
    email_domain = lead["email"].split("@")[-1]
    lead["domain"] = domain_of(lead["website"]) if (email_domain in FREE and lead["website"]) else email_domain
    lead["website"] = lead["website"] or (f"https://{email_domain}" if email_domain not in FREE else "")
    return lead


def is_suppressed(db, email: str, domain: str) -> str | None:
    r = db.execute("select reason from suppressions where value in (?, ?)", (email, domain)).fetchone()
    return r[0] if r else None


def upsert(db, lead: dict, source: str, raw: dict, import_id: int | None = None) -> tuple[str, int | None, list[str]]:
    """'created', 'merged' (blank fields filled from the new source), 'invalid' or 'suppressed'."""
    if not EMAIL.match(lead["email"]):
        return "invalid", None, []
    if is_suppressed(db, lead["email"], lead["domain"]):
        return "suppressed", None, []
    cols = ["first_name", "last_name", "title", "company", "domain", "website", "country", "linkedin"]
    old = db.execute("select * from leads where email = ?", (lead["email"],)).fetchone()
    if old:
        filled = [c for c in cols if not old[c] and lead[c]]  # never overwrite what we have: first source wins, blanks get filled
        if filled:
            db.execute(f"update leads set {', '.join(c + ' = ?' for c in filled)}, updated_at = ? where id = ?",
                       [lead[c] for c in filled] + [now(), old["id"]])
        lid, outcome = old["id"], "merged"
        event(db, lid, "merged", f"seen again in {source}" + (f", filled {', '.join(filled)}" if filled else ""))
    else:
        lid = db.execute(f"insert into leads (email, {', '.join(cols)}, created_at, updated_at) values (?, {', '.join('?' * len(cols))}, ?, ?)",
                         [lead["email"]] + [lead[c] for c in cols] + [now(), now()]).lastrowid
        outcome, filled = "created", []
        event(db, lid, "created", f"from {source}")
    db.execute("insert into lead_sources (lead_id, source, import_id, raw, at) values (?, ?, ?, ?, ?)", (lid, source, import_id, json.dumps(raw), now()))
    return outcome, lid, filled


def import_csv(db, filename: str, data: bytes, by: str) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")  # Excel on Windows
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = {key(h): h for h in reader.fieldnames or []}
    mapping = {f: next((headers[a] for a in aliases if a in headers), None) for f, aliases in FIELDS.items()}
    if not mapping["email"]:
        raise BadFile(f"No email column found. Columns were: {', '.join(reader.fieldnames or [])}")
    fmt = next((name for name, marks in FORMATS.items() if marks <= set(headers)), "spreadsheet")
    db.execute("begin immediate")
    try:
        iid = db.execute("insert into imports (filename, fmt, rows, created, merged, invalid, suppressed, at, by) values (?, ?, 0, 0, 0, 0, 0, ?, ?)",
                         (filename[:200], fmt, now(), by)).lastrowid
        counts, report = {"created": 0, "merged": 0, "invalid": 0, "suppressed": 0}, []
        rows = 0
        for i, raw in enumerate(reader, start=2):
            if not any((v or "").strip() for v in raw.values()):
                continue
            rows += 1
            lead = normalize({f: raw.get(col) for f, col in mapping.items() if col})
            outcome, lid, filled = upsert(db, lead, f"import #{iid} ({filename})", raw, iid)
            counts[outcome] += 1
            if outcome != "created":
                report.append({"line": i, "email": lead["email"] or "(empty)", "outcome": outcome, "filled": filled})
        db.execute("update imports set rows = ?, created = ?, merged = ?, invalid = ?, suppressed = ?, report = ? where id = ?",
                   (rows, counts["created"], counts["merged"], counts["invalid"], counts["suppressed"], json.dumps({"mapping": mapping, "rows": report}), iid))
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise
    return {"id": iid, "format": fmt, "rows": rows, **counts, "mapping": mapping}
