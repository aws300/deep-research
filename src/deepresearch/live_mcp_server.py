"""Streaming MCP server (hosted on AgentCore Runtime, port 8000, stateless streamable HTTP).

Default interactive mode for the deep-research service:
  research(query, depth)       submit + stream progress notifications (reasoning / tool steps) until the report is ready;
                               client disconnect or cancellation stops the research (StopRuntimeSession).
  watch_research(task_id)      attach to a running task and stream its steps (resume after a 15-min gateway cut).
  cancel_research(task_id)     stop immediately.
Progress is delivered as MCP progress notifications (when the client sent a progressToken) and as log messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import boto3
from botocore.config import Config
from mcp.server.fastmcp import Context, FastMCP

from .cancel import TERMINAL, cancel_task
from .config import load_settings
from .dedupe import DedupeClient
from .queue import IntakeQueue
from .task_store import TaskStore

log = logging.getLogger("deepresearch.live")
S = load_settings()
STORE = TaskStore(S)
QUEUE = IntakeQueue(S)
DEDUPE = DedupeClient(S, STORE, QUEUE)
S3 = boto3.client("s3", region_name=S.harness_region, endpoint_url=f"https://s3.{S.harness_region}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
MAX_INLINE = 60_000
STREAM_BUDGET = 810          # seconds; stay under the gateway's 15-minute tool-call limit
POLL = 2
HEARTBEAT = 20        # seconds without a step before a keep-alive progress notification

mcp = FastMCP("nx-deep-research-live", host="0.0.0.0", port=8000, stateless_http=True,
              instructions="Deep research. Default: `research` (ASYNC: returns task_id immediately; equivalent requests are merged "
                           "into one task). Then `watch_research(task_id)` streams the live steps. `research_live` = submit+stream "
                           "in one call. `cancel_research(task_id, subscription_id)` detaches you; backend stops only when nobody "
                           "else is attached.")


def _fmt(ev: dict) -> str | None:
    k = ev.get("kind")
    if k == "tool_use":
        tool = (ev.get("tool") or "").split("___")[-1]
        return f"🔧 {tool}: {ev.get('arg') or ''}"[:300]
    if k == "reasoning":
        return f"💭 {(ev.get('text') or '')[:600]}"
    if k == "text":
        return f"💬 {(ev.get('text') or '')[:400]}"
    if k in ("system", "cancelled"):
        return f"⚙️ {ev.get('text')}"
    return None  # tool_result previews are noisy; skip


def _report(task: dict) -> dict:
    uri = (task.get("result") or {}).get("report_s3")
    out = {"task_id": task["task_id"], "status": task.get("status"), "progress": task.get("progress"), "report_s3": uri}
    if uri:
        bucket, key = uri[5:].split("/", 1)
        body = S3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        out.update(format="markdown", truncated=len(body) > MAX_INLINE, content=body[:MAX_INLINE],
                   download_url=S3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600))
    return out


async def _stream(ctx: Context, task_id: str, budget: float, cancel_on_disconnect: bool, subscription_id: str | None = None) -> dict:
    seen: set[str] = set()
    n = 0
    t0 = time.time()
    last_sent = time.time()
    try:
        while True:
            events = await asyncio.to_thread(STORE.list_events, task_id, 500)
            for ev in events:
                if ev["sk"] in seen:
                    continue
                seen.add(ev["sk"])
                line = _fmt(ev)
                if line:
                    n += 1
                    await ctx.report_progress(progress=n, total=None, message=line)
                    await ctx.info(line)
                    last_sent = time.time()
            task = await asyncio.to_thread(STORE.get_task, task_id) or {}
            if time.time() - last_sent >= HEARTBEAT and task.get("status") not in TERMINAL:
                # keep idle SSE connections alive (queued tasks can be silent for minutes) and tell the user why
                n += 1
                hb = f"⏳ {task.get('status', 'queued')} {int(time.time() - t0)}s (waiting for next step)"
                await ctx.report_progress(progress=n, total=None, message=hb)
                await ctx.info(hb)   # also as a log notification: bytes must flow even if the client sent no progressToken
                last_sent = time.time()
            if task.get("status") in TERMINAL:
                log.info("stream end task=%s status=%s events=%d duration=%.0fs", task_id, task["status"], n, time.time() - t0)
                await ctx.info(f"✅ {task_id} {task['status']}")
                return _report(task)
            if time.time() - t0 > budget:
                await ctx.info("⏳ stream window ended; task keeps running. Call watch_research(task_id) to re-attach.")
                return {"task_id": task_id, "status": task.get("status"), "progress": task.get("progress"),
                        "note": "still running; call watch_research(task_id) to keep watching, or cancel_research(task_id)"}
            await asyncio.sleep(POLL)
    except asyncio.CancelledError:
        # client went away or sent notifications/cancelled -> interrupt the research itself
        log.info("stream cancelled by client task=%s events=%d after %.0fs cancel_on_disconnect=%s", task_id, n, time.time() - t0, cancel_on_disconnect)
        if cancel_on_disconnect:
            r = await asyncio.to_thread(cancel_task, S, task_id, STORE, "client disconnected / cancelled the stream", subscription_id)
            log.info("task %s after client disconnect -> %s", task_id, r)
        raise


@mcp.tool()
async def research(query: str, depth: str = "quick", callback_url: str | None = None, cache: bool = True) -> dict:
    """DEFAULT (async). Submit a deep research and return immediately with task_id. With cache=true (default) semantically
    equivalent requests (e.g. '昨天的科技新闻' vs '最近1天的科技新闻') within 12 h are merged into the same task_id (merged=true);
    cache=false always starts a fresh task. Follow with watch_research(task_id) to stream the steps, or pass callback_url."""
    if depth not in ("quick", "standard", "deep"):
        return {"error": "depth must be quick|standard|deep"}
    res = await asyncio.to_thread(DEDUPE.resolve, cache=cache, query=query, depth=depth, actor_id="mcp-live", callback_url=callback_url, source="mcp-live")
    t = await asyncio.to_thread(STORE.get_task, res["task_id"]) or {}
    log.info("research %s task=%s depth=%s reason=%s query=%r", "MERGED" if res["merged"] else "submitted", res["task_id"], depth, res.get("reason"), query[:120])
    return {"task_id": res["task_id"], "status": t.get("status", "queued"), "depth": t.get("depth", depth), "merged": res["merged"],
            "subscription_id": res["subscription_id"], "canonical": res.get("canonical"), "reason": res.get("reason"),
            "next": f"watch_research('{res['task_id']}') streams live steps; cancel_research(task_id, subscription_id='{res['subscription_id']}') detaches you"}


@mcp.tool()
async def research_live(query: str, depth: str = "quick", cache: bool = True, ctx: Context = None) -> dict:  # type: ignore[assignment]
    """SYNC-SAFE MODE. Submit (cache=true merges with an equivalent recent task) and stream the live steps in this single
    blocking call; returns the Markdown report (quick ~4-7 min, standard/deep ~10-15 min). Keep-alive notifications every
    20 s keep the connection open. If the task outlives the ~13.5-min window it returns 'still running' -> watch_research.
    Cancelling this call detaches you; the shared backend stops only if nobody else is attached."""
    if depth not in ("quick", "standard", "deep"):
        return {"error": "depth must be quick|standard|deep"}
    res = await asyncio.to_thread(DEDUPE.resolve, cache=cache, query=query, depth=depth, actor_id="mcp-live", source="mcp-live")
    tid = res["task_id"]
    log.info("research_live %s task=%s sub=%s", "MERGED" if res["merged"] else "submitted", tid, res["subscription_id"])
    await ctx.info(f"🚀 {'attached to existing' if res['merged'] else 'submitted'} {tid} (depth={depth})")
    await ctx.report_progress(progress=0, total=None, message=f"submitted {tid}")
    return await _stream(ctx, tid, STREAM_BUDGET, cancel_on_disconnect=True, subscription_id=res["subscription_id"])


@mcp.tool()
async def watch_research(task_id: str, cancel_on_disconnect: bool = False, ctx: Context = None) -> dict:  # type: ignore[assignment]
    """Attach to an existing task and stream its steps until it finishes (or the 13-min window ends)."""
    t = await asyncio.to_thread(STORE.get_task, task_id)
    if not t:
        return {"error": f"unknown task_id {task_id}"}
    if t.get("status") in TERMINAL:
        return _report(t)
    return await _stream(ctx, task_id, STREAM_BUDGET, cancel_on_disconnect=cancel_on_disconnect)


@mcp.tool()
async def cancel_research(task_id: str, subscription_id: str | None = None, force: bool = False) -> dict:
    """Detach from (subscription_id) or cancel a research task. The shared backend is stopped only when no other
    subscriber remains, or force=true."""
    return await asyncio.to_thread(cancel_task, S, task_id, STORE, "cancel_research tool", subscription_id, bool(force))


@mcp.tool()
async def research_status(task_id: str) -> dict:
    """Status + progress counters + report path (no streaming)."""
    t = await asyncio.to_thread(STORE.get_task, task_id)
    if not t:
        return {"error": f"unknown task_id {task_id}"}
    return {k: t.get(k) for k in ("task_id", "status", "depth", "progress", "result", "last_error", "created_at", "updated_at")}


if __name__ == "__main__":
    import os
    logging.basicConfig(level=os.environ.get("DR_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("live MCP server starting; table=%s queue=%s bucket=%s", S.table_name, QUEUE.url, S.bucket)
    mcp.run(transport="streamable-http")
