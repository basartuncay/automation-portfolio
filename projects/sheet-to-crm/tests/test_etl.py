import json
from pathlib import Path

import httpx
import pytest

from sheet_to_crm.core import Airtable, convert, read_rows, transform

ROOT = Path(__file__).parents[1]
MAPPING = json.loads((ROOT / "mapping.json").read_text())


def test_sample_file_validation_and_dedupe():
    res = transform(read_rows(ROOT / "samples" / "companies.csv"), MAPPING)
    assert [r["Domain"] for r in res.records] == ["brightwave-foods.example", "northlane.example"]
    assert res.duplicates == 1
    assert res.records[0]["Name"] == "Brightwave Foods Inc." and res.records[0]["Annual Revenue"] == 4800000  # last row wins
    assert res.records[1]["First Contact"] == "2026-05-14"  # 14.05.2026 normalised
    errors = {r["row"]: r["errors"] for r in res.rejects}
    assert errors[4] == "Company Name: required"
    assert "Employees" in errors[5]
    assert "First Contact" in errors[7]  # 31/02 is not a real date


def test_xlsx_input(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Company Name", "Website"])
    wb.active.append(["Acme", "acme.example"])
    wb.save(tmp_path / "x.xlsx")
    assert read_rows(tmp_path / "x.xlsx") == [{"Company Name": "Acme", "Website": "acme.example"}]


@pytest.mark.parametrize("raw,kind,out", [
    ("https://www.Acme.example/about", "domain", "acme.example"),
    ("$1,250", "number", 1250), ("3.5", "number", 3.5), ("  ", "text", None), ("03/31/2026", "date", "2026-03-31"),
])
def test_convert(raw, kind, out):
    assert convert(raw, kind) == out


def mock_airtable(handler):
    return Airtable("appX", "Companies", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleep=lambda s: None)


def test_upsert_batches_of_ten_and_counts():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        assert req.method == "PATCH" and req.headers["Authorization"] == "Bearer tok"
        n = len(body["records"])
        return httpx.Response(200, json={"createdRecords": ["x"] * (n - 1), "updatedRecords": ["y"], "records": []})

    recs = [{"Domain": f"d{i}.example", "Name": f"N{i}"} for i in range(23)]
    created, updated = mock_airtable(handler).upsert(recs, "Domain")
    assert [len(b["records"]) for b in bodies] == [10, 10, 3]
    assert bodies[0]["performUpsert"] == {"fieldsToMergeOn": ["Domain"]}
    assert (created, updated) == (20, 3)


def test_retries_rate_limit_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"createdRecords": ["a"], "updatedRecords": []})

    assert mock_airtable(handler).upsert([{"Domain": "a"}], "Domain") == (1, 0)
    assert len(calls) == 2


def test_bad_request_stops_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(422, json={"error": {"type": "UNKNOWN_FIELD_NAME"}})

    with pytest.raises(RuntimeError, match="422"):
        mock_airtable(handler).upsert([{"Domain": "a"}], "Domain")
    assert len(calls) == 1
