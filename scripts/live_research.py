#!/usr/bin/env python
"""Watch a deep research live from the terminal (and use it as the streaming e2e test).

  python scripts/live_research.py "研究问题"                      # stream steps with timestamps; Ctrl+C cancels the research
  python scripts/live_research.py "问题" --depth quick --save ./reports/x.md
  python scripts/live_research.py --watch <task_id>              # re-attach to a running task
  python scripts/live_research.py "问题" --assert                # e2e mode: exit 1 unless reasoning/text steps stream in
                                                                 # within --first-event-timeout seconds and a report is returned

Talks MCP 2026-07-28 (stateless) over streamable HTTP/SSE to the Gateway, SigV4-signed with the mcp_client.aws_profile profile
(falls back to default credentials). Progress arrives as notifications/progress + notifications/message.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.config import load_settings  # noqa: E402

S = load_settings()
URL = S.state("gateway_url")
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "nx-live-cli", "version": "0.1"},
        "io.modelcontextprotocol/clientCapabilities": {}}


def _creds():
    prof = os.environ.get("DR_MCP_PROFILE") or S.raw.get("mcp_client", {}).get("aws_profile", "")
    try:
        c = boto3.Session(profile_name=prof).get_credentials() if prof else None
    except Exception:  # noqa: BLE001  profile not configured -> default credentials
        c = None
    return c or boto3.Session().get_credentials()


def _signed(method: str, params: dict, name: str | None, accept: str):
    body = json.dumps({"jsonrpc": "2.0", "id": int(time.time()), "method": method, "params": {"_meta": {**META, "progressToken": "cli"}, **params}}).encode()
    headers = {"Content-Type": "application/json", "Accept": accept, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    req = AWSRequest(method="POST", url=URL, data=body, headers=headers)
    SigV4Auth(_creds(), "bedrock-agentcore", S.gateway_region).add_auth(req)
    return urllib.request.Request(URL, data=body, headers=dict(req.headers), method="POST")


def call_json(name: str, arguments: dict) -> dict:
    with urllib.request.urlopen(_signed("tools/call", {"name": name, "arguments": arguments}, name, "application/json"), timeout=120) as r:
        out = json.loads(r.read().decode())
    return json.loads(out["result"]["content"][0]["text"])


a_verbose = [False]


def stream(name: str, arguments: dict, on_line, timeout: int = 900) -> dict | None:
    """Yield progress lines to on_line(kind, text, t_rel); return the final tool result (parsed) or None."""
    t0 = time.time()
    final = None
    with urllib.request.urlopen(_signed("tools/call", {"name": name, "arguments": arguments}, name, "text/event-stream, application/json"), timeout=timeout) as resp:
        if "text/event-stream" not in resp.headers.get("Content-Type", ""):
            return json.loads(json.loads(resp.read().decode())["result"]["content"][0]["text"])
        buf: list[str] = []
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "" and buf:
                msg = json.loads("".join(buf)); buf = []
                m = msg.get("method")
                if a_verbose[0]:
                    print("RAW", json.dumps(msg, ensure_ascii=False)[:500], flush=True)
                if m == "notifications/progress":
                    on_line("progress", msg["params"].get("message", ""), time.time() - t0)
                elif m == "notifications/message":
                    d = msg["params"].get("data")
                    on_line("log", d if isinstance(d, str) else json.dumps(d, ensure_ascii=False), time.time() - t0)
                elif "result" in msg:
                    final = json.loads(msg["result"]["content"][0]["text"])
                    break  # result received; don't block on stream close
                elif "error" in msg:
                    on_line("error", json.dumps(msg["error"], ensure_ascii=False), time.time() - t0)
    return final


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--depth", default="quick", choices=["quick", "standard", "deep"])
    ap.add_argument("--watch", metavar="TASK_ID")
    ap.add_argument("--save", metavar="PATH", help="write the Markdown report here")
    ap.add_argument("--assert", dest="assert_mode", action="store_true", help="e2e mode with pass/fail exit code")
    ap.add_argument("--first-event-timeout", type=int, default=90)
    ap.add_argument("--timeout", type=int, default=3600, help="total seconds to keep watching (auto re-attaches after each ~13-min stream window)")
    ap.add_argument("--cache", default="true", help="true (default): merge with an equivalent recent task; false: force a fresh task")
    ap.add_argument("--verbose", action="store_true", help="print raw JSON-RPC notifications too")
    ap.add_argument("--log", metavar="FILE", help="append every streamed line (with timestamps) to this file")
    a = ap.parse_args()
    logf = open(a.log, "a", encoding="utf-8") if a.log else None
    if not URL:
        print("gateway not deployed", file=sys.stderr); return 2
    if not a.query and not a.watch:
        ap.error("query or --watch required")

    task_id = {"id": a.watch}
    seen = {"progress": 0, "reasoning": 0, "text": 0, "tool": 0, "first_at": None}
    interrupted = {"v": False}

    def on_line(kind, text, t_rel):
        if kind == "log" and seen["progress"]:  # log duplicates progress; print progress only when both arrive
            return
        seen["progress"] += 1
        if seen["first_at"] is None:
            seen["first_at"] = t_rel
        if text.startswith("⏳"):
            seen["progress"] -= 1  # keep-alive, not a research step
        if text.startswith("💭"): seen["reasoning"] += 1
        elif text.startswith("💬"): seen["text"] += 1
        elif text.startswith("🔧"): seen["tool"] += 1
        m = re.search(r"submitted (t\d+-[0-9a-f]+)", text)
        if m:
            task_id["id"] = m.group(1)
        line = f"[{t_rel:6.1f}s] {text}"
        print(line, flush=True)
        if logf:
            logf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {kind} {line}\n"); logf.flush()

    def on_sigint(*_):
        interrupted["v"] = True
        print("\n^C  -> cancelling research ...", flush=True)
        if task_id["id"]:
            print(json.dumps(call_json("live___cancel_research", {"task_id": task_id["id"]}), ensure_ascii=False))
        sys.exit(130)

    signal.signal(signal.SIGINT, on_sigint)
    a_verbose[0] = a.verbose
    use_cache = str(a.cache).strip().lower() not in ("false", "0", "no", "off")
    t_start = time.time()
    if a.watch:
        task_id["id"] = a.watch
        final = stream("live___watch_research", {"task_id": a.watch}, on_line, min(900, a.timeout))
    else:
        print(f"▶ research depth={a.depth} cache={use_cache}: {a.query}")
        final = stream("live___research_live", {"query": a.query, "depth": a.depth, "cache": use_cache}, on_line, min(900, a.timeout))
    # long tasks: the gateway caps one streamed call at ~15 min -> keep re-attaching until terminal or --timeout
    while final and final.get("status") not in ("completed", "completed_no_report", "failed", "cancelled") and task_id["id"] \
            and time.time() - t_start < a.timeout:
        print(f"[{time.time()-t_start:6.0f}s] ↻ re-attaching to {task_id['id']} (status={final.get('status')})", flush=True)
        final = stream("live___watch_research", {"task_id": task_id["id"]}, on_line, min(900, a.timeout))

    if task_id["id"]:
        print(f"\ntask_id={task_id['id']}  (debug: python scripts/debug_task.py {task_id['id']})")
    print("\n=== summary ===")
    print(f"events={seen['progress']} tool_steps={seen['tool']} reasoning_steps={seen['reasoning']} narration_steps={seen['text']} "
          f"first_event_at={seen['first_at']}s")
    ok = bool(final) and final.get("status") == "completed" and "content" in final
    if final:
        print(f"status={final.get('status')} report_s3={final.get('report_s3')} download_url={'yes' if final.get('download_url') else 'no'}")
        if a.save and final.get("content"):
            Path(a.save).parent.mkdir(parents=True, exist_ok=True)
            Path(a.save).write_text(final["content"])
            print(f"saved -> {a.save} ({len(final['content'])} chars)")
    if a.assert_mode:
        checks = {
            "streamed_events": seen["progress"] >= 3,
            "first_event_within_timeout": seen["first_at"] is not None and seen["first_at"] <= a.first_event_timeout,
            "thinking_visible": (seen["reasoning"] + seen["text"]) >= 1,
            "report_completed": ok,
            "report_has_bibliography": bool(final and "## Bibliography" in (final.get("content") or "")),
        }
        print("checks:", json.dumps(checks))
        return 0 if all(checks.values()) else 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
