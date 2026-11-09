import subprocess, sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoice_extractor.api import app

ROOT = Path(__file__).parents[1]
client = TestClient(app)


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("s")
    subprocess.run([sys.executable, str(ROOT / "make_samples.py"), str(out)], check=True, capture_output=True)
    return out


def post(path: Path, **headers):
    return client.post("/extract", files={"file": (path.name, path.read_bytes(), "application/pdf")}, headers=headers)


def test_extract_ok(samples):
    r = post(samples / "acme_1.pdf").json()
    assert r["ok"] and r["number"] == "INV-01042" and r["total"] == 202.78 and len(r["lines"]) == 2


def test_extract_problem_is_reported_not_500(samples):
    r = post(samples / "acme_2_broken.pdf")
    assert r.status_code == 200 and r.json()["ok"] is False and "subtotal + tax != total" in r.json()["problems"]


def test_api_key_enforced_when_set(samples, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_API_KEY", "k")
    assert post(samples / "acme_1.pdf").status_code == 401
    assert post(samples / "acme_1.pdf", **{"X-API-Key": "k"}).status_code == 200


def test_filename_cannot_escape_tempdir(samples):
    r = client.post("/extract", files={"file": ("../../etc/evil.pdf", (samples / "acme_1.pdf").read_bytes(), "application/pdf")})
    assert r.status_code == 200 and r.json()["file"] == "evil.pdf"
