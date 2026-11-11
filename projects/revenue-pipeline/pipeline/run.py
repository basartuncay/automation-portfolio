"""The pipeline run: extract every source, build and test the warehouse with dbt, and publish only what passed.

The dashboard never reads the working warehouse. After a fully green run, the warehouse is checkpointed and copied
to serving.duckdb with an atomic rename. A failed run (a source down, a renamed CSV column, a data test that finds
negative spend) changes nothing the dashboard shows: it keeps serving the last verified data and says so.
"""
import json, logging, os, shutil, sqlite3, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx

from .extract import extract_all, now_batch

log = logging.getLogger("pipeline")
ROOT = Path(__file__).parents[1]
DBT_DIR = ROOT / "warehouse"

RUNS_DDL = """
create table if not exists runs (id integer primary key, started text not null, finished text, trigger text not null,
  status text not null check (status in ('running', 'success', 'failed')), sources text, models integer, tests_passed integer, skipped integer,
  failures text, published integer not null default 0, seconds real);
create table if not exists requests (id integer primary key, at text not null, done integer not null default 0);
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def paths() -> dict:
    data = Path(os.environ.get("DATA_DIR", "data"))
    data.mkdir(parents=True, exist_ok=True)
    return {"warehouse": data / "warehouse.duckdb", "serving": data / "serving.duckdb", "runs": data / "runs.db"}


def runs_db() -> sqlite3.Connection:
    db = sqlite3.connect(paths()["runs"], timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.executescript(RUNS_DDL)
    return db


def dbt_build(warehouse: Path) -> dict:
    """Run `dbt build` (models + tests, in dependency order) in its own process and summarise run_results.json."""
    with tempfile.TemporaryDirectory() as target:
        env = {**os.environ, "WAREHOUSE_PATH": str(warehouse), "DBT_TARGET_PATH": target}
        proc = subprocess.run([str(Path(sys.executable).parent / "dbt"), "build", "--project-dir", str(DBT_DIR),
                               "--profiles-dir", str(DBT_DIR), "--quiet"], env=env, capture_output=True, text=True)
        results_file = Path(target) / "run_results.json"
        if not results_file.exists():
            return {"ok": False, "models": 0, "tests_passed": 0, "skipped": 0,
                    "failures": [{"name": "dbt", "message": (proc.stdout + proc.stderr).strip()[-600:]}]}
        results = json.loads(results_file.read_text())["results"]
    models = [r for r in results if r["unique_id"].startswith("model.")]
    tests = [r for r in results if r["unique_id"].startswith("test.")]
    failures = [{"name": r["unique_id"].split(".")[-1] if r["unique_id"].startswith("model.") else r["unique_id"].split(".")[2],
                 "status": r["status"], "rows": r.get("failures"), "message": (r.get("message") or "")[:300]}
                for r in results if r["status"] in ("error", "fail")]
    skipped = sum(r["status"] == "skipped" for r in results)  # downstream of a failure: not built, not a cause
    return {"ok": proc.returncode == 0 and not failures, "models": sum(r["status"] == "success" for r in models),
            "tests_passed": sum(r["status"] == "pass" for r in tests), "skipped": skipped, "failures": failures}


def publish(warehouse: Path, serving: Path) -> None:
    con = duckdb.connect(str(warehouse))
    con.execute("checkpoint")  # fold the WAL into the file before copying it
    con.close()
    tmp = serving.with_suffix(".tmp")
    shutil.copyfile(warehouse, tmp)
    os.replace(tmp, serving)  # atomic: readers see the old file or the new one, never half of one


def run_once(trigger: str = "schedule", http: httpx.Client | None = None) -> dict:
    p, runs = paths(), runs_db()
    rid = runs.execute("insert into runs (started, trigger, status) values (?, ?, 'running')", (now(), trigger)).lastrowid
    runs.commit()
    t0 = time.monotonic()
    own_http = http is None
    http = http or httpx.Client(timeout=30)
    try:
        con = duckdb.connect(str(p["warehouse"]))
        try:
            sources = extract_all(con, http, os.environ["SOURCES_URL"].rstrip("/"), os.environ["SOURCES_API_TOKEN"], now_batch())
        finally:
            con.close()
    finally:
        if own_http:
            http.close()
    errors = [{"name": name, "message": s["error"]} for name, s in sources.items() if "error" in s]
    if errors:  # incomplete data: don't build marts on it, don't publish
        build = {"ok": False, "models": 0, "tests_passed": 0, "skipped": 0, "failures": errors}
    else:
        build = dbt_build(p["warehouse"])
    if build["ok"]:
        publish(p["warehouse"], p["serving"])
    status = "success" if build["ok"] else "failed"
    runs.execute("""update runs set finished = ?, status = ?, sources = ?, models = ?, tests_passed = ?, skipped = ?, failures = ?,
                    published = ?, seconds = ? where id = ?""",
                 (now(), status, json.dumps(sources), build["models"], build["tests_passed"], build["skipped"], json.dumps(build["failures"]),
                  int(build["ok"]), round(time.monotonic() - t0, 1), rid))
    runs.commit()
    runs.close()
    log.info("run %s %s: %s", rid, status, build["failures"] or f"{build['models']} models, {build['tests_passed']} tests passed")
    return {"id": rid, "status": status, **build, "sources": sources}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    every = float(os.environ.get("RUN_EVERY_MINUTES", 60)) * 60
    last = 0.0
    while True:
        db = runs_db()
        asked = db.execute("select id from requests where done = 0").fetchall()
        if asked or time.time() - last >= every:
            db.executemany("update requests set done = 1 where id = ?", [(r["id"],) for r in asked])
            db.commit()
            db.close()
            run_once("manual" if asked else "schedule")
            last = time.time()
        else:
            db.close()
        time.sleep(2)


if __name__ == "__main__":
    main()
