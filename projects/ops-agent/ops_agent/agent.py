"""The agent loop. Every model turn and tool call is written to an append-only trace, and the conversation is saved
after each turn, so a run that waits for a manager's approval can resume hours later, after a restart, exactly where
it stopped."""
import json, time

from . import db as store
from . import tools as toolbox

MAX_TURNS = 12
TOOL_DEFS = [t.for_claude() for t in toolbox.TOOLS.values()]


def step(conn, run_id: int, kind: str, name: str | None = None, data=None, ms: int | None = None) -> None:
    conn.execute("insert into steps (run_id, at, kind, name, data, ms) values (?, ?, ?, ?, ?, ?)",
                 (run_id, store.now(), kind, name, json.dumps(data) if data is not None else None, ms))


def create_run(conn, task: str, source: str, engine_name: str, actor: str) -> int:
    rid = conn.execute("insert into runs (task, source, engine, status, created_at, messages) values (?, ?, ?, 'queued', ?, ?)",
                       (task.strip(), source, engine_name, store.now(), json.dumps([{"role": "user", "content": task.strip()}]))).lastrowid
    step(conn, rid, "task", actor, {"task": task.strip()})
    conn.commit()
    return rid


def finish(conn, run_id: int, status: str, summary: str | None) -> None:
    conn.execute("update runs set status = ?, summary = ?, finished_at = ?, pending = null where id = ?",
                 (status, summary, store.now(), run_id))
    step(conn, run_id, status if status in ("done", "failed") else "note", None, {"summary": summary})
    conn.commit()


def tool_result(tool_use_id: str, outcome: dict) -> dict:
    body = {k: v for k, v in outcome.items()}
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(body, default=str),
            "is_error": outcome["status"] == "error"}


def advance(conn, engine, run_id: int) -> str:
    """Drive the run until it finishes or waits for approval. Returns the run's new status."""
    run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    messages = json.loads(run["messages"])
    conn.execute("update runs set status = 'running' where id = ?", (run_id,))
    conn.commit()
    for _ in range(MAX_TURNS - run["turns"]):
        t0 = time.monotonic()
        try:
            resp = engine.create(messages, TOOL_DEFS)
        except Exception as e:  # network/API trouble: keep the run and its trace, mark it failed with the reason
            finish(conn, run_id, "failed", f"model call failed: {type(e).__name__}: {e}"[:400])
            return "failed"
        ms = int((time.monotonic() - t0) * 1000)
        content = resp["content"]
        messages.append({"role": "assistant", "content": content})
        text = " ".join(b["text"] for b in content if b["type"] == "text").strip()
        uses = [b for b in content if b["type"] == "tool_use"]
        step(conn, run_id, "model", engine.name, {"text": text, "tool_calls": [{"name": u["name"], "input": u["input"]} for u in uses],
                                                   "usage": resp["usage"], "stop_reason": resp["stop_reason"]}, ms)
        conn.execute("""update runs set messages = ?, turns = turns + 1, input_tokens = input_tokens + ?,
                        output_tokens = output_tokens + ? where id = ?""",
                     (json.dumps(messages), resp["usage"].get("input_tokens", 0), resp["usage"].get("output_tokens", 0), run_id))
        conn.commit()
        if resp["stop_reason"] == "refusal":
            finish(conn, run_id, "failed", "The model declined this task.")
            return "failed"
        if resp["stop_reason"] == "max_tokens":
            finish(conn, run_id, "failed", "The model's answer was cut off.")
            return "failed"
        if not uses:
            finish(conn, run_id, "done", text or "Done.")
            return "done"
        results, waiting = [], []
        for u in uses:
            t0 = time.monotonic()
            out = toolbox.call(conn, u["name"], u["input"], actor=f"agent (run {run_id})", run_id=run_id, tool_use_id=u["id"])
            step(conn, run_id, "tool", u["name"], {"input": u["input"], **out}, int((time.monotonic() - t0) * 1000))
            if out["status"] == "needs_approval":
                waiting.append(out["approval_id"])
            else:
                results.append(tool_result(u["id"], out))
        conn.commit()
        if waiting:
            conn.execute("update runs set status = 'waiting_approval', pending = ? where id = ?", (json.dumps(results), run_id))
            step(conn, run_id, "waiting", None, {"approvals": waiting})
            conn.commit()
            return "waiting_approval"
        messages.append({"role": "user", "content": results})
        conn.execute("update runs set messages = ? where id = ?", (json.dumps(messages), run_id))
        conn.commit()
    finish(conn, run_id, "failed", f"Stopped after {MAX_TURNS} turns without finishing.")
    return "failed"


def decide(conn, approval_id: int, user, approve: bool, note: str = "") -> int | None:
    """Record a manager's decision and run (or refuse) the call. Returns the run id to resume once its turn is complete."""
    # claim it first, atomically: two managers clicking at once can't both run the action
    if not conn.execute("update approvals set decided_by = ? where id = ? and status = 'pending' and decided_by is null",
                        (user["name"], approval_id)).rowcount:
        return None
    conn.commit()
    a = conn.execute("select * from approvals where id = ?", (approval_id,)).fetchone()
    if approve:
        out = toolbox.call(conn, a["tool"], json.loads(a["args"]), actor=a["requested_by"], run_id=a["run_id"],
                           tool_use_id=a["tool_use_id"], approved_by=user["name"])
    else:
        out = {"status": "error", "error": f"A manager ({user['name']}) rejected this action" + (f": {note.strip()}" if note.strip() else ".")}
    conn.execute("update approvals set status = ?, decided_by = ?, decided_at = ?, note = ?, result = ? where id = ?",
                 ("approved" if approve else "rejected", user["name"], store.now(), note.strip() or None, json.dumps(out, default=str), approval_id))
    if a["run_id"]:
        step(conn, a["run_id"], "approval", a["tool"], {"approval_id": approval_id, "decision": "approved" if approve else "rejected",
                                                        "by": user["name"], "note": note.strip() or None, **out})
    conn.commit()
    if not a["run_id"]:
        return None
    still = conn.execute("select count(*) from approvals where run_id = ? and status = 'pending'", (a["run_id"],)).fetchone()[0]
    return None if still else a["run_id"]


def resume(conn, engine, run_id: int) -> str:
    """All approvals of the waiting turn are decided: hand every tool result back to the model, in call order, and continue."""
    run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    if run["status"] != "waiting_approval":
        return run["status"]
    messages = json.loads(run["messages"])
    done = {r["tool_use_id"]: r for r in json.loads(run["pending"] or "[]")}
    for a in conn.execute("select * from approvals where run_id = ? and status != 'pending'", (run_id,)):
        if a["tool_use_id"] not in done:
            done[a["tool_use_id"]] = tool_result(a["tool_use_id"], json.loads(a["result"]))
    order = [b["id"] for b in messages[-1]["content"] if b["type"] == "tool_use"]
    messages.append({"role": "user", "content": [done[i] for i in order]})
    conn.execute("update runs set messages = ?, pending = null where id = ?", (json.dumps(messages), run_id))
    conn.commit()
    return advance(conn, engine, run_id)


def cost_usd(run) -> float:
    from .engine import PRICE
    return (run["input_tokens"] * PRICE["input"] + run["output_tokens"] * PRICE["output"]) / 1_000_000
