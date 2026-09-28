"""Lambda target for the Gateway: exposes the research pipeline as MCP tools.

Tools (schema in infra/mcp_tools.py):
  submit_research(query, depth)         -> {task_id, status}
  get_research_status(task_id)          -> {status, progress, recent_steps, report_s3}
  get_research_report(task_id, mode)    -> markdown content (mode=content) or presigned URL (mode=url)
The Gateway passes tool arguments as the event and the tool name in context.client_context.custom.
"""
from __future__ import annotations

import json
import logging
import os
import time

import boto3

from .cancel import cancel_task
from .config import load_settings
from .dedupe import DedupeClient
from .queue import IntakeQueue
from .task_store import TaskStore

log = logging.getLogger("deepresearch.mcp_tools")
log.setLevel(os.environ.get("DR_LOG_LEVEL", "INFO"))
_S = load_settings()
_STORE = TaskStore(_S)
_QUEUE = IntakeQueue(_S, queue_url=os.environ.get("QUEUE_URL"))
_DEDUPE = DedupeClient(_S, _STORE, _QUEUE)   # non-VPC Lambda cannot reach Valkey -> auto falls back to the VPC dedupe Lambda
_S3 = boto3.client("s3", region_name=_S.harness_region,
                   endpoint_url=f"https://s3.{_S.harness_region}.amazonaws.com",
                   config=boto3.session.Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
SYNC_WAIT_CAP = int(os.environ.get("DR_SYNC_WAIT_CAP", "300"))
MAX_INLINE = 60_000  # keep well under Claude Code's default 25k-token MCP output cap


def submit_research(query: str, depth: str = "standard", actor_id: str = "mcp", callback_url: str | None = None, cache: bool = True) -> dict:
    if depth not in ("quick", "standard", "deep"):
        return {"error": "depth must be quick|standard|deep"}
    if not query or len(query) > 4000:
        return {"error": "query is required (<= 4000 chars)"}
    if callback_url and not (callback_url.startswith("https://") or callback_url.startswith("http://127.0.0.1")
                             or callback_url.startswith("http://localhost")):
        return {"error": "callback_url must be https:// (http only for loopback testing)"}
    res = _DEDUPE.resolve(cache=_truthy(cache), query=query, depth=depth, actor_id=actor_id, callback_url=callback_url, source="mcp")
    t = _STORE.get_task(res["task_id"]) or {}
    out = {"task_id": res["task_id"], "status": t.get("status", "queued"), "depth": t.get("depth", depth),
           "merged": res["merged"], "subscription_id": res["subscription_id"], "canonical": res.get("canonical"),
           "eta_minutes": {"quick": "3-6", "standard": "10-20", "deep": "20-40"}[t.get("depth", depth)]}
    if res["merged"]:
        out["note"] = ("an equivalent research task is already running/finished; you were attached to it. "
                       "cancel_research(task_id, subscription_id) only detaches you.")
    if callback_url:
        out["callback"] = ("on completion a signed JSON POST (X-NX-Signature: sha256=HMAC) with task_id, status, report_s3 and a "
                           "1h download_url is sent to callback_url; no polling needed")
    else:
        out["hint"] = "no callback_url given: use run_research for quick tasks, or call get_research_status later"
    return out


def run_research(query: str, depth: str = "quick", wait_seconds: int = 300, cache: bool = True) -> dict:
    """Synchronous variant: submit and block until the report is ready (or wait_seconds elapses). Intended for quick depth."""
    sub = submit_research(query, depth=depth, cache=_truthy(cache))
    if "error" in sub:
        return sub
    tid = sub["task_id"]
    # non-streamed responses are dropped by the network path after ~350 s of silence (measured), so never block longer
    deadline = time.time() + max(30, min(int(wait_seconds), SYNC_WAIT_CAP))
    st = {}
    while time.time() < deadline:
        st = get_research_status(tid)
        if st.get("status") in ("completed", "completed_no_report", "failed"):
            break
        time.sleep(10)
    if st.get("status") == "completed":
        rep = get_research_report(tid, mode="content")
        rep["status"] = "completed"
        rep["download_url"] = get_research_report(tid, mode="url")["download_url"]
        rep["progress"] = st.get("progress")
        return rep
    if st.get("status") == "failed":
        return {"task_id": tid, "status": "failed", "error": st.get("last_error")}
    return {"task_id": tid, "status": st.get("status", "running"), "progress": st.get("progress"),
            "note": (f"still running after {int(min(int(wait_seconds), SYNC_WAIT_CAP))}s (sync cap). Continue with "
                     f"live___watch_research('{tid}') for a streamed wait, or get_research_report(task_id) later. "
                     "For a single blocking call that always completes, use live___research_live.")}


def get_research_status(task_id: str) -> dict:
    t = _STORE.get_task(task_id)
    if not t:
        return {"error": f"unknown task_id {task_id}"}
    steps = []
    for e in _STORE.list_events(task_id, limit=400)[-10:]:
        k = e.get("kind")
        if k == "tool_use":
            steps.append({"step": "tool_use", "tool": (e.get("tool") or "").split("___")[-1], "arg": e.get("arg")})
        elif k == "reasoning":
            steps.append({"step": "reasoning", "text": (e.get("text") or "")[:300]})
    res = t.get("result") or {}
    return {"task_id": task_id, "status": t.get("status"), "depth": t.get("depth"), "progress": t.get("progress"),
            "recent_steps": steps, "report_s3": res.get("report_s3"), "last_error": t.get("last_error"),
            "created_at": t.get("created_at"), "updated_at": t.get("updated_at")}


def get_research_report(task_id: str, mode: str = "content") -> dict:
    t = _STORE.get_task(task_id)
    if not t:
        return {"error": f"unknown task_id {task_id}"}
    uri = (t.get("result") or {}).get("report_s3")
    if not uri:
        return {"task_id": task_id, "status": t.get("status"), "error": "report not ready"}
    bucket, key = uri[5:].split("/", 1)
    if mode == "url":
        url = _S3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        return {"task_id": task_id, "report_s3": uri, "download_url": url, "expires_in": 3600}
    body = _S3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    truncated = len(body) > MAX_INLINE
    return {"task_id": task_id, "report_s3": uri, "format": "markdown", "truncated": truncated,
            "content": body[:MAX_INLINE]}


def _truthy(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() not in ("false", "0", "no", "off", "")


def cancel_research(task_id: str, subscription_id: str | None = None, force: bool = False) -> dict:
    return cancel_task(_S, task_id, store=_STORE, subscription_id=subscription_id, force=bool(force))


TOOLS = {"submit_research": submit_research, "run_research": run_research, "cancel_research": cancel_research,
         "get_research_status": get_research_status, "get_research_report": get_research_report}


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "")
    name = full.split("___")[-1] if "___" in full else full
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {full}", "available": list(TOOLS)}
    args = event if isinstance(event, dict) else json.loads(event or "{}")
    t0 = time.time()
    log.info("tool=%s args=%s request_id=%s", name, json.dumps({k: (str(v)[:200]) for k, v in args.items()}, ensure_ascii=False),
             custom.get("bedrockAgentCoreAwsRequestId"))
    try:
        out = fn(**{k: v for k, v in args.items() if k in fn.__code__.co_varnames})
        log.info("tool=%s done in %.1fs -> %s", name, time.time() - t0, json.dumps({k: (str(v)[:120]) for k, v in out.items() if k != "content"}, ensure_ascii=False))
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("tool=%s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}
