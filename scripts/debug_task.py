#!/usr/bin/env python
"""One-stop debugging view for a research task.

  python scripts/debug_task.py <task_id>                 # record + timeline of events + callbacks + where the logs are
  python scripts/debug_task.py <task_id> --logs          # also tail CloudWatch logs of the harness session, live MCP server, Lambda
  python scripts/debug_task.py --latest [--logs]         # most recent task
  python scripts/debug_task.py --list 10                 # last N tasks with status
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.aws_clients import client  # noqa: E402
from deepresearch.config import load_settings  # noqa: E402
from deepresearch.task_store import TaskStore  # noqa: E402

S = load_settings()
STORE = TaskStore(S)


def _ts(v) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(v)


def list_tasks(n: int) -> list[dict]:
    r = STORE.table.scan(FilterExpression="begins_with(pk, :p) AND sk = :m", ExpressionAttributeValues={":p": "DR#TASK#", ":m": "meta"})
    items = sorted(r["Items"], key=lambda x: int(x["created_at"]))[-n:]
    for t in items:
        print(f"{t['task_id']}  {t['status']:<20} {_ts(t['created_at'])}  depth={t.get('depth')}  {str(t.get('query'))[:60]!r}")
    return items


def _harness_runtime_id() -> str | None:
    rid = S.state("harness_runtime_id")
    if rid:
        return rid
    try:
        h = client("bedrock-agentcore-control", S.harness_region).get_harness(harnessId=S.state("harness_id"))["harness"]
        rid = h["environment"]["agentCoreRuntimeEnvironment"]["agentRuntimeId"]
        S.set_state(harness_runtime_id=rid)
        return rid
    except Exception:  # noqa: BLE001
        return None


def log_groups(task: dict) -> dict:
    rid = _harness_runtime_id()
    return {
        "harness_runtime": f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT" if rid else None,
        "live_mcp_runtime": f"/aws/bedrock-agentcore/runtimes/{(S.state('live_runtime_id') or '')}-DEFAULT" if S.state("live_runtime_id") else None,
        "mcp_tools_lambda": f"/aws/lambda/{S['project']}-mcp-tools",
        "dispatcher": "local process stdout / --log-file (or EKS pod logs)",
    }


def tail(group: str, region: str, since_epoch: int, pattern: str | None, limit: int = 60) -> None:
    logs = client("logs", region)
    try:
        kw = {"logGroupName": group, "startTime": since_epoch * 1000, "limit": limit, "interleaved": True}
        if pattern:
            kw["filterPattern"] = f'"{pattern}"'
        evs = logs.filter_log_events(**kw).get("events", [])
    except Exception as e:  # noqa: BLE001
        print(f"  (cannot read {group}: {e})")
        return
    if not evs:
        print("  (no matching log events)")
    for e in evs[-limit:]:
        print(f"  {datetime.fromtimestamp(e['timestamp']/1000).strftime('%H:%M:%S')} {e['message'].rstrip()[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--list", type=int, metavar="N")
    ap.add_argument("--logs", action="store_true")
    ap.add_argument("--events", type=int, default=80, help="max events to print")
    ap.add_argument("--requeue", action="store_true", help="mark the task retry and push it back to SQS (orphaned/failed tasks)")
    ap.add_argument("--cancel", action="store_true", help="cancel the task (StopRuntimeSession)")
    a = ap.parse_args()
    if a.list:
        list_tasks(a.list); return 0
    if a.latest:
        a.task_id = list_tasks(1)[-1]["task_id"]
    if not a.task_id:
        ap.error("task_id, --latest or --list required")
    t = STORE.get_task(a.task_id)
    if not t:
        print("unknown task"); return 1
    if a.cancel:
        from deepresearch.cancel import cancel_task
        print(json.dumps(cancel_task(S, a.task_id, STORE, "debug_task --cancel"), ensure_ascii=False)); t = STORE.get_task(a.task_id)
    if a.requeue:
        from deepresearch.queue import IntakeQueue
        STORE.update_task(a.task_id, status="retry", last_error="requeued via debug_task")
        IntakeQueue(S).send({"task_id": a.task_id, "query": t.get("query", ""), "depth": t.get("depth", "standard"), "actor_id": t.get("actor_id", "anonymous")})
        print("requeued", a.task_id); t = STORE.get_task(a.task_id)
    print("=== task ===")
    for k in ("task_id", "status", "depth", "actor_id", "created_at", "started_at", "finished_at", "updated_at", "attempts", "worker",
              "session_id", "harness_arn", "progress", "result", "callbacks", "last_error", "cancel_reason", "metadata"):
        if k in t:
            v = t[k]
            if k.endswith("_at"):
                v = f"{v} ({_ts(v)})"
            print(f"{k:12} {json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v}")
    print(f"query        {t.get('query')!r}")
    evs = STORE.list_events(a.task_id, limit=1000)
    print(f"\n=== events ({len(evs)}) ===")
    for e in evs[-a.events:]:
        ts = e["sk"].split("#")[1]
        k = e.get("kind")
        body = e.get("arg") or e.get("text") or e.get("preview") or ""
        tool = (e.get("tool") or "").split("___")[-1]
        print(f"{_ts(ts)} {k:<11} {tool:<14} {str(body)[:160].replace(chr(10), ' ')}")
    groups = log_groups(t)
    print("\n=== where to look ===")
    for k, v in groups.items():
        print(f"{k:18} {v}")
    print(f"report/evidence     s3://{S.bucket}/reports/YYYY/MM/DD/{a.task_id}/  (report.md, sources.jsonl, evidence.jsonl)")
    print(f"AgentCore console   CloudWatch > GenAI Observability > Harnesses / Runtime sessions (session {t.get('session_id')})")
    if a.logs:
        since = int(t.get("created_at", time.time() - 3600)) - 60
        if groups["harness_runtime"]:
            print(f"\n=== harness runtime logs (session {t.get('session_id')}) ===")
            tail(groups["harness_runtime"], S.harness_region, since, t.get("session_id"))
        if groups["live_mcp_runtime"]:
            print("\n=== live MCP server logs ===")
            tail(groups["live_mcp_runtime"], S.harness_region, since, a.task_id)
        print("\n=== MCP tools Lambda logs ===")
        tail(groups["mcp_tools_lambda"], S.gateway_region, since, a.task_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
