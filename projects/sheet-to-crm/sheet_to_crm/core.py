"""Spreadsheet -> validated, deduplicated records -> Airtable upsert (create or update by a key field)."""
import csv, json, logging, time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import httpx

log = logging.getLogger("sheet_to_crm")
BATCH = 10  # Airtable accepts at most 10 records per write request
MIN_INTERVAL = 0.22  # Airtable allows 5 requests/second per base


def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        ws = load_workbook(path, read_only=True, data_only=True).active
        rows = ws.iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in next(rows)]
        return [dict(zip(head, r)) for r in rows if any(v not in (None, "") for v in r)]
    with open(path, newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel's CSV export adds a BOM
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def convert(value, kind: str):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if kind == "text":
        return str(value).strip()
    if kind == "lower":
        return str(value).strip().lower()
    if kind == "domain":
        v = str(value).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
        return v.split("/")[0]
    if kind == "number":
        if isinstance(value, (int, float)):
            return value
        v = str(value).strip().replace(",", "").replace("$", "").replace("€", "")
        return float(v) if "." in v else int(v)
    if kind == "date":
        if isinstance(value, (datetime, date)):
            return value.strftime("%Y-%m-%d")
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(value).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        raise ValueError(f"unrecognised date {value!r}")
    raise ValueError(f"unknown type {kind}")


@dataclass
class Result:
    records: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    duplicates: int = 0


def transform(rows: list[dict], mapping: dict) -> Result:
    """mapping: {"key": "<target field>", "fields": {"<source column>": {"to": "<target>", "type": "...", "required": bool}}}"""
    res, seen = Result(), {}
    for n, row in enumerate(rows, start=2):  # row 1 is the header
        rec, errors = {}, []
        for src, spec in mapping["fields"].items():
            try:
                v = convert(row.get(src), spec.get("type", "text"))
            except ValueError as e:
                errors.append(f"{src}: {e}")
                continue
            if v is None and spec.get("required"):
                errors.append(f"{src}: required")
            elif v is not None:
                rec[spec["to"]] = v
        if errors:
            res.rejects.append({"row": n, "errors": "; ".join(errors), **{k: row.get(k) for k in mapping["fields"]}})
            continue
        key = rec[mapping["key"]]
        if key in seen:  # last row wins, like a spreadsheet edited over time
            res.duplicates += 1
            res.records[seen[key]] = rec
        else:
            seen[key] = len(res.records)
            res.records.append(rec)
    return res


class Airtable:
    def __init__(self, base_id: str, table: str, token: str, client: httpx.Client | None = None, sleep=time.sleep):
        self.url = f"https://api.airtable.com/v0/{base_id}/{table}"
        self.client = client or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.sleep, self._last = sleep, 0.0

    def _patch(self, body: dict, retries: int = 5) -> dict:
        for attempt in range(retries):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            r = self.client.patch(self.url, headers=self.headers, content=json.dumps(body))
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", 0)) or 2 ** attempt * (30 if r.status_code == 429 else 1)
                log.warning("HTTP %s, retrying in %ss", r.status_code, delay)  # Airtable asks 30s after a 429
                self.sleep(delay)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Airtable {r.status_code}: {r.text[:300]}")  # bad field name/type: stop, don't retry
            return r.json()
        raise RuntimeError("Airtable kept failing after retries")

    def upsert(self, records: list[dict], key: str) -> tuple[int, int]:
        created = updated = 0
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            data = self._patch({"performUpsert": {"fieldsToMergeOn": [key]}, "typecast": True,
                                "records": [{"fields": r} for r in chunk]})
            created += len(data.get("createdRecords", []))
            updated += len(data.get("updatedRecords", []))
        return created, updated
