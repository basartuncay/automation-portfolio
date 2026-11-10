import asyncio, json, os, subprocess, sys
from pathlib import Path

import pytest

from src import main as actor_main
from src.extractor import Result

ROOT = Path(__file__).parents[1]


def test_extractor_copy_matches_the_library():
    # The Apify build context only contains this folder, so the extractor is vendored; it must not drift.
    assert (ROOT / "src" / "extractor.py").read_text() == (ROOT.parent / "ai-extractor" / "ai_extractor" / "core.py").read_text()


def run_actor(tmp_path, monkeypatch, inp, fake_extract=None):
    kvs = tmp_path / "key_value_stores" / "default"
    kvs.mkdir(parents=True)
    (kvs / "INPUT.json").write_text(json.dumps(inp))
    monkeypatch.setenv("CRAWLEE_STORAGE_DIR", str(tmp_path))
    if fake_extract:
        monkeypatch.setattr(actor_main, "extract", fake_extract)
    with pytest.raises(SystemExit) as exit_:
        asyncio.run(actor_main.main())
    items = sorted((tmp_path / "datasets" / "default").glob("0*.json")) if (tmp_path / "datasets").exists() else []
    return exit_.value.code, [json.loads(p.read_text()) for p in items]


INPUT = {"startUrls": [{"url": "https://a.example/1"}, {"url": "https://a.example/2"}],
         "schema": {"type": "object", "properties": {"name": {"type": "string"}}}, "anthropicApiKey": "sk-test"}


def test_successful_and_failed_pages_are_both_listed(tmp_path, monkeypatch):
    def fake(client, http, url, schema, instructions=""):
        assert schema["additionalProperties"] is False  # strictified before use
        return Result(url, data={"name": "ok"}) if url.endswith("1") else Result(url, error="fetch failed: 404")

    code, items = run_actor(tmp_path, monkeypatch, INPUT, fake)
    assert code == 0
    assert [(i["url"], i["data"], i["error"]) for i in items] == [
        ("https://a.example/1", {"name": "ok"}, None), ("https://a.example/2", None, "fetch failed: 404")]


def run_subprocess(tmp_path, inp):
    """A fresh process per run: the Apify SDK caches its storage location for the life of the process."""
    kvs = tmp_path / "key_value_stores" / "default"
    kvs.mkdir(parents=True)
    (kvs / "INPUT.json").write_text(json.dumps(inp))
    p = subprocess.run([sys.executable, "-m", "src"], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "CRAWLEE_STORAGE_DIR": str(tmp_path)})
    return p.returncode, p.stdout + p.stderr


def test_missing_input_fails_clearly(tmp_path):
    code, log = run_subprocess(tmp_path, {"startUrls": []})
    assert code == 1 and "Provide startUrls, schema and anthropicApiKey" in log


def test_non_object_schema_fails_clearly(tmp_path):
    code, log = run_subprocess(tmp_path, {**INPUT, "schema": {"type": "array"}})
    assert code == 1 and "Invalid schema" in log
