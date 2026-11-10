"""Eval suite: each case runs the agent on a fresh copy of the shop, plays the manager when asked, then checks the END STATE
of the database (refunds, statuses, messages sent) rather than the wording of the reply. Wording can vary from run to
run; money moved or not moved is what matters.

    python -m ops_agent.evals                 # current engine (offline without a key, Claude with ANTHROPIC_API_KEY)
    python -m ops_agent.evals --out report.json
"""
import argparse, json, os, sys, tempfile, time
from pathlib import Path

from . import agent
from . import db as store
from .engine import make_engine

CASES = Path(__file__).parents[1] / "evals" / "cases.json"


def run_case(case: dict, engine, workdir: str) -> dict:
    path = os.path.join(workdir, f"{case['id']}.db")
    conn = store.connect(path)
    store.seed_shop(conn)
    store.seed_users(conn)
    manager = conn.execute("select * from users where role = 'manager'").fetchone()
    t0 = time.monotonic()
    rid = agent.create_run(conn, case["task"], "eval", engine.name, "eval")
    status = agent.advance(conn, engine, rid)
    for _ in range(4):  # play the manager for every approval the run asks for
        if status != "waiting_approval":
            break
        pend = conn.execute("select id from approvals where run_id = ? and status = 'pending'", (rid,)).fetchall()
        nxt = None
        for a in pend:
            nxt = agent.decide(conn, a["id"], manager, bool(case.get("approve")), "" if case.get("approve") else "not justified")
        status = agent.resume(conn, engine, nxt) if nxt else status
    run = conn.execute("select * from runs where id = ?", (rid,)).fetchone()
    failures = []
    if status != case["expect_status"]:
        failures.append(f"run ended {status}, expected {case['expect_status']}")
    for sql, want in case["checks"]:
        got = conn.execute(sql).fetchone()
        got = got[0] if got else None
        if got != want:
            failures.append(f"{sql} -> {got!r}, expected {want!r}")
    conn.close()
    return {"id": case["id"], "task": case["task"], "passed": not failures, "failures": failures, "status": status,
            "summary": run["summary"], "turns": run["turns"], "input_tokens": run["input_tokens"],
            "output_tokens": run["output_tokens"], "cost_usd": round(agent.cost_usd(run), 4),
            "seconds": round(time.monotonic() - t0, 2)}


def run_all(engine=None, only: str | None = None) -> dict:
    engine = engine or make_engine()
    cases = [c for c in json.loads(CASES.read_text()) if not only or c["id"] == only]
    with tempfile.TemporaryDirectory() as tmp:
        results = [run_case(c, engine, tmp) for c in cases]
    return {"engine": engine.name, "at": store.now(), "passed": sum(r["passed"] for r in results), "total": len(results),
            "cost_usd": round(sum(r["cost_usd"] for r in results), 4), "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("EVAL_REPORT", "eval-report.json"))
    ap.add_argument("--only")
    args = ap.parse_args()
    report = run_all(only=args.only)
    Path(args.out).write_text(json.dumps(report, indent=1))
    for r in report["results"]:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<32} {r['turns']} turns  {r['summary']}")
        for f in r["failures"]:
            print(f"      {f}")
    print(f"{report['passed']}/{report['total']} passed with {report['engine']} (cost ${report['cost_usd']:.4f})")
    sys.exit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
