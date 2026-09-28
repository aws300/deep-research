"""Runs one research task end-to-end on the harness and persists progress/artifacts."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from .aws_clients import client
from .config import Settings
from .harness_client import HarnessClient, new_session_id, summarize_tool_input
from .task_store import TaskStore

DEPTH_PROFILE = {
    "quick": {"searches": "6-10", "sections": "3", "words": "800-1500", "iterations": 40},
    "standard": {"searches": "15-25", "sections": "4-6", "words": "2000-4000", "iterations": 90},
    "deep": {"searches": "30-45", "sections": "6-8", "words": "4000-8000", "iterations": 150},
}

TASK_PROMPT = """Research request (task_id={task_id}, depth={depth}):

{query}

Requirements:
- Use the deep-research-harness skill workflow: SCOPE -> PLAN -> RETRIEVE (issue {searches} WebSearch calls in total;
  fire independent queries in the same turn so they run in parallel) -> TRIANGULATE -> SYNTHESIZE -> WRITE -> VALIDATE.
- Persist every source and quote with the skill scripts (sources.jsonl / evidence.jsonl) under {workspace}/{task_id}/.
- Write the final report to {workspace}/{task_id}/report.md with {sections} main sections, {words} words,
  numbered inline citations [N] and a complete "## Bibliography" listing every cited URL.
- Run scripts/validate_report.py on the report and fix issues it reports.
- Finish your final message with exactly one line:
  DR_STATUS {{"report_path": "...", "sources": <int>, "searches": <int>, "validated": <true|false>}}
"""


log = logging.getLogger("deepresearch.worker")


class ThrottledError(RuntimeError):
    """Raised when AgentCore/Bedrock throttles the invocation (dispatcher applies backpressure)."""


def _is_throttle(e: ClientError) -> bool:
    code = e.response.get("Error", {}).get("Code", "")
    return code in ("ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException",
                    "RuntimeClientError") or e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 429


class ResearchWorker:
    def __init__(self, settings: Settings, store: TaskStore | None = None, harness: HarnessClient | None = None):
        self.s = settings
        self.store = store or TaskStore(settings)
        self.harness = harness or HarnessClient(settings)
        self.s3 = client("s3", settings.harness_region)
        # regional endpoint + SigV4 so presigned URLs do not 307-redirect (curl would otherwise save the redirect XML)
        from botocore.config import Config
        from .aws_clients import _SESSION, _LOCK
        with _LOCK:
            self.presign = _SESSION.client("s3", region_name=settings.harness_region,
                                           endpoint_url=f"https://s3.{settings.harness_region}.amazonaws.com",
                                           config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
        self.flush_every = float(settings["dispatcher"]["progress_flush_seconds"])

    def run(self, task: dict, worker_id: str = "local") -> dict:
        task_id = task["task_id"]
        depth = task.get("depth", "standard")
        prof = DEPTH_PROFILE.get(depth, DEPTH_PROFILE["standard"])
        session_id = new_session_id()
        prompt = TASK_PROMPT.format(task_id=task_id, depth=depth, query=task["query"], workspace=self.s.workspace_dir,
                                    searches=prof["searches"], sections=prof["sections"], words=prof["words"])
        t_start = time.time()
        log.info("[%s] start depth=%s session=%s worker=%s query=%r", task_id, depth, session_id, worker_id, task["query"][:120])
        for attempt in range(3):   # a stuck microVM session is abandoned for a fresh one
            self.store.update_task(task_id, session_id=session_id, harness_arn=self.harness.arn)
            try:
                t_b = time.time()
                tools_dir = self.bootstrap_session(session_id)
                log.info("[%s] bootstrap ok in %.1fs (attempt %d)", task_id, time.time() - t_b, attempt + 1)
                break
            except (TimeoutError, RuntimeError) as e:
                log.warning("[%s] bootstrap attempt %d failed on session %s: %s", task_id, attempt + 1, session_id, str(e)[:200])
                if attempt == 2:
                    raise
                session_id = new_session_id()
        prompt += f"\nThe skill scripts are pre-installed at {tools_dir}/ (SKILL_DIR={tools_dir}). Do not search for them.\n"
        overrides = {"maxIterations": prof["iterations"]}
        buf: list[dict] = []
        last_flush = time.time()
        text_parts: list[str] = []
        reasoning_acc: list[str] = []   # aggregated per flush window -> one "reasoning" event
        narration_acc: list[str] = []   # assistant text between tool calls -> one "text" event
        stats = {"searches": 0, "tool_calls": 0, "reasoning_chars": 0, "input_tokens": 0, "output_tokens": 0}
        stop_reason = ""
        first_event_at = None
        try:
            for ev in self._invoke_with_resume(prompt, session_id, task.get("actor_id"), overrides):
                t = ev["type"]
                if first_event_at is None:
                    first_event_at = time.time()
                    log.info("[%s] first harness event after %.1fs", task_id, first_event_at - t_start)
                if t == "text":
                    text_parts.append(ev["text"])
                    narration_acc.append(ev["text"])
                elif t == "reasoning":
                    stats["reasoning_chars"] += len(ev["text"])
                    reasoning_acc.append(ev["text"])
                elif t == "tool_use":
                    if reasoning_acc:
                        buf.append({"kind": "reasoning", "text": "".join(reasoning_acc)[:3000]}); reasoning_acc = []
                    if narration_acc:
                        buf.append({"kind": "text", "text": "".join(narration_acc)[-1500:]}); narration_acc = []
                    stats["tool_calls"] += 1
                    if ev.get("name", "").endswith("WebSearch"):
                        stats["searches"] += 1
                    buf.append({"kind": "tool_use", "tool": ev.get("name"), "arg": summarize_tool_input(ev.get("input", ""))})
                elif t == "tool_result":
                    buf.append({"kind": "tool_result", "tool_use_id": ev.get("tool_use_id"),
                                "preview": json.dumps(ev.get("content"))[:300]})
                elif t == "usage":
                    stats["input_tokens"] += ev["input_tokens"]
                    stats["output_tokens"] += ev["output_tokens"]
                elif t == "stop":
                    stop_reason = ev["reason"]
                elif t == "error":
                    raise RuntimeError(f"{ev['kind']}: {ev['message']}")
                if time.time() - last_flush > self.flush_every:
                    if reasoning_acc:
                        buf.append({"kind": "reasoning", "text": "".join(reasoning_acc)[:3000]}); reasoning_acc = []
                    if narration_acc and len("".join(narration_acc)) > 80:
                        buf.append({"kind": "text", "text": "".join(narration_acc)[-1500:]}); narration_acc = []
                if buf and time.time() - last_flush > self.flush_every:
                    self.store.append_events(task_id, buf)
                    log.info("[%s] progress searches=%d tool_calls=%d in_tok=%d out_tok=%d events+%d", task_id,
                             stats["searches"], stats["tool_calls"], stats["input_tokens"], stats["output_tokens"], len(buf))
                    cur = (self.store.get_task(task_id) or {}).get("status")
                    if cur == "cancelled":
                        raise RuntimeError("cancelled")
                    self.store.update_task(task_id, progress=stats, status="running")
                    buf, last_flush = [], time.time()
        except ClientError as e:
            if (self.store.get_task(task_id) or {}).get("status") == "cancelled":
                log.info("[%s] cancelled (harness session stopped) after %.0fs", task_id, time.time() - t_start)
                self.store.append_events(task_id, buf + [{"kind": "system", "text": "harness session stopped after cancel"}])
                return {"task_id": task_id, "session_id": session_id, "status": "cancelled", "stats": stats}
            if _is_throttle(e):
                log.warning("[%s] throttled by AgentCore/Bedrock: %s", task_id, str(e)[:300])
                raise ThrottledError(str(e)) from e
            log.error("[%s] harness error: %s", task_id, str(e)[:500])
            raise
        except Exception:
            if (self.store.get_task(task_id) or {}).get("status") == "cancelled":
                return {"task_id": task_id, "session_id": session_id, "status": "cancelled", "stats": stats}
            raise
        finally:
            if reasoning_acc:
                buf.append({"kind": "reasoning", "text": "".join(reasoning_acc)[:3000]})
            if narration_acc:
                buf.append({"kind": "text", "text": "".join(narration_acc)[-1500:]})
            if buf:
                self.store.append_events(task_id, buf)

        if (self.store.get_task(task_id) or {}).get("status") == "cancelled":
            return {"task_id": task_id, "session_id": session_id, "status": "cancelled", "stats": stats}
        final_text = "".join(text_parts)
        status_line = _parse_status(final_text)
        report_key = self._collect_report(task_id, session_id, status_line)
        result = {"task_id": task_id, "session_id": session_id, "stop_reason": stop_reason, "stats": stats,
                  "status_line": status_line, "report_s3": report_key, "final_text_chars": len(final_text)}
        status = "completed" if report_key else "completed_no_report"
        self.store.update_task(task_id, status=status, result=result, finished_at=int(time.time()), progress=stats)
        log.info("[%s] %s in %.0fs stop=%s searches=%d tool_calls=%d in_tok=%d out_tok=%d report=%s", task_id, status,
                 time.time() - t_start, stop_reason, stats["searches"], stats["tool_calls"], stats["input_tokens"],
                 stats["output_tokens"], report_key)
        if not report_key:
            log.warning("[%s] no report collected; status_line=%s final_text_tail=%r", task_id, status_line, final_text[-300:])
        cb = self.notify(task, status, result)
        if cb:
            log.info("[%s] callbacks: %s", task_id, cb)
        return result

    # --------------------------------------------------------------- callbacks
    def notify(self, task: dict, status: str, result: dict | None, error: str | None = None) -> dict:
        """Completion callback: HTTPS webhook (metadata.callback_url, HMAC-SHA256 signed) + SNS topic (if configured).
        Never raises: callback failures are recorded on the task, not treated as task failures."""
        # the dispatcher hands us the SQS message (task_id/query/depth/actor_id); metadata such as callback_url lives in DynamoDB
        try:
            full = self.store.get_task(task["task_id"]) or {}
        except Exception:  # noqa: BLE001
            full = {}
        task = {**full, **{k: v for k, v in task.items() if v is not None}, "metadata": full.get("metadata") or task.get("metadata") or {}}
        payload = {"event": "research.task." + ("completed" if status.startswith("completed") else "failed"),
                   "task_id": task["task_id"], "status": status, "depth": task.get("depth"), "query": task.get("query"),
                   "actor_id": task.get("actor_id"), "report_s3": (result or {}).get("report_s3"), "error": error,
                   "finished_at": datetime.now(timezone.utc).isoformat()}
        uri = payload["report_s3"]
        if uri:
            bucket, key = uri[5:].split("/", 1)
            payload["download_url"] = self.s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        outcome: dict = {}
        urls = []
        cb = (task.get("metadata") or {}).get("callback_url") or task.get("callback_url")
        if cb:
            urls.append(cb)
        try:  # merged tasks: every active subscription gets its own callback
            urls += [s["callback_url"] for s in self.store.list_subscriptions(task["task_id"]) if s.get("callback_url")]
        except Exception:  # noqa: BLE001
            pass
        for i, u in enumerate(dict.fromkeys(urls)):
            outcome[f"webhook{i or ''}"] = f"{u} -> {self._post_webhook(u, payload)}"
        topic = self.s.state("sns_topic_arn")
        if topic:
            try:
                client("sns", self.s.queue_region).publish(
                    TopicArn=topic, Subject=f"deep-research {payload['event']} {task['task_id']}",
                    Message=json.dumps(payload, ensure_ascii=False, default=str),
                    MessageAttributes={"event": {"DataType": "String", "StringValue": payload["event"]},
                                       "actor_id": {"DataType": "String", "StringValue": str(task.get("actor_id") or "anonymous")}})
                outcome["sns"] = "published"
            except Exception as e:  # noqa: BLE001
                outcome["sns"] = f"error: {e}"[:300]
        if outcome:
            try:
                self.store.update_task(task["task_id"], callbacks=outcome)
            except Exception:  # noqa: BLE001
                pass
        return outcome

    def _post_webhook(self, url: str, payload: dict, attempts: int = 3) -> str:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        secret = self.s.webhook_secret
        headers = {"Content-Type": "application/json", "User-Agent": "deepresearch/1.0",
                   "X-NX-Event": payload["event"], "X-NX-Task-Id": payload["task_id"]}
        if secret:
            headers["X-NX-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        last = ""
        for i in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return f"{resp.status}"
            except Exception as e:  # noqa: BLE001
                last = f"error: {e}"[:200]
                log.warning("webhook attempt %d/%d to %s failed: %s", i + 1, attempts, url, last)
                time.sleep(2 * (i + 1))
        return last

    # ----------------------------------------------------------------- helpers
    RESUMABLE = ("maximum token limit", "partial_turn", "max_tokens", "continue by calling the agent again")
    # transport-level breaks of the long-lived event stream (NAT / proxy / server resets): the session and its files survive
    TRANSIENT = ("response ended prematurely", "connection broken", "incompleteread", "read timed out", "connection reset",
                 "protocolerror", "responsestreamingerror", "connection aborted", "remote end closed")
    BUSY = ("conflictexception", "already in progress", "currently processing")   # previous turn still running

    @classmethod
    def _is_transient(cls, e: BaseException) -> bool:
        s = f"{type(e).__name__} {e}".lower()
        return any(k in s for k in cls.TRANSIENT + cls.BUSY)

    def _invoke_with_resume(self, prompt: str, session_id: str, actor_id, overrides: dict, max_resumes: int = 6,
                            max_reconnects: int = 4):
        """Stream the harness on one runtimeSessionId and keep the task alive across two kinds of interruptions:
          - output limit: the harness keeps the partial message and asks the caller to invoke again -> 'Continue.'
          - stream break (connection dropped mid-turn): back off, re-invoke the same session with a resume prompt;
            a still-running previous turn answers 'busy' -> wait and retry."""
        from botocore.exceptions import EventStreamError
        turn_prompt, resumes, reconnects = prompt, 0, 0
        while True:
            try:
                for ev in self.harness.invoke(turn_prompt, session_id, actor_id=actor_id, overrides=overrides):
                    if ev["type"] == "stop" and ev["reason"] in ("max_tokens", "partial_turn") and resumes < max_resumes:
                        break  # fall through to resume below
                    yield ev
                else:
                    return
            except EventStreamError as e:
                if any(k in str(e) for k in self.RESUMABLE) and resumes < max_resumes:
                    pass
                elif self._is_transient(e) and reconnects < max_reconnects:
                    reconnects += 1
                    yield from self._reconnect_notice(e, reconnects)
                    turn_prompt = self.STREAM_RESUME
                    continue
                else:
                    raise
            except Exception as e:  # noqa: BLE001
                if not self._is_transient(e) or reconnects >= max_reconnects:
                    raise
                reconnects += 1
                yield from self._reconnect_notice(e, reconnects)
                turn_prompt = self.STREAM_RESUME
                continue
            resumes += 1
            yield {"type": "reasoning", "text": f"[service] output limit reached, resuming turn {resumes}"}
            turn_prompt = ("Continue exactly where you stopped. If you were writing a file, append the remaining content "
                           "with file_operations (str_replace/insert/append), then carry on with the workflow.")

    STREAM_RESUME = ("The connection to the orchestrator was interrupted; your workspace files and the conversation so far are "
                     "intact. Check the workspace (report.md, sources/evidence files) and continue the research workflow exactly "
                     "where you left off. Do not restart from scratch.")

    def _reconnect_notice(self, e: BaseException, n: int):
        wait = min(15 * n, 60)
        log.warning("harness stream interrupted (%s: %s) -> re-attaching to the same session in %ss (%d)",
                    type(e).__name__, str(e)[:200], wait, n)
        yield {"type": "reasoning", "text": f"[service] stream interrupted ({type(e).__name__}); re-attaching to the session in {wait}s"}
        time.sleep(wait)

    def bootstrap_session(self, session_id: str) -> str:
        """Materialise the skill scripts inside the microVM before the first model turn (no tokens spent).

        The harness delivers SKILL.md text to the model but does not write scripts/ to disk, so we install them via
        InvokeAgentRuntimeCommand. Scripts are written inline (base64, ~40 KB < 64 KB command limit): no network access
        from the microVM is needed (an S3/NAT fetch without timeouts used to hang sessions for 15+ minutes).
        """
        import base64
        from .infra.skills import SKILL_DIR
        tools_dir = f"{self.s.workspace_dir}/tools"
        names = ("citation_manager.py", "evidence_store.py", "validate_report.py")
        parts = [f"mkdir -p {tools_dir}"]
        for n in names:
            b64 = base64.b64encode((SKILL_DIR / "scripts" / n).read_bytes()).decode()
            parts.append(f"echo '{b64}' | base64 -d > {tools_dir}/{n}")
        parts += [f"python3 -m py_compile {tools_dir}/{n}" for n in names] + [f"ls {tools_dir}"]
        code, out = self.harness.run_command(session_id, " && ".join(parts), timeout=60, wall_timeout=120)
        if code != 0 or "validate_report.py" not in out:
            log.error("skill bootstrap failed (session %s): %s", session_id, out[:300])
            raise RuntimeError(f"skill bootstrap failed: {out[:300]}")
        return tools_dir

    def _collect_report(self, task_id: str, session_id: str, status_line: dict | None) -> str | None:
        """Pull report + evidence files out of the microVM via the command API and upload to S3."""
        base = f"{self.s.workspace_dir}/{task_id}"
        candidates = [status_line.get("report_path")] if status_line and status_line.get("report_path") else []
        candidates += [f"{base}/report.md"]
        prefix = self.s["storage"]["reports_prefix"] + f"{datetime.now(timezone.utc):%Y/%m/%d}/{task_id}/"
        uploaded = None
        for path in candidates:
            try:
                content = self.harness.read_file(session_id, path)
            except Exception:
                content = None
            if content:
                self.s3.put_object(Bucket=self.s.bucket, Key=prefix + "report.md", Body=content.encode(),
                                   ContentType="text/markdown; charset=utf-8")
                uploaded = f"s3://{self.s.bucket}/{prefix}report.md"
                break
        for fname in ("sources.jsonl", "evidence.jsonl", "claims.jsonl"):
            try:
                content = self.harness.read_file(session_id, f"{base}/{fname}")
                if content:
                    self.s3.put_object(Bucket=self.s.bucket, Key=prefix + fname, Body=content.encode())
            except Exception:
                pass
        return uploaded


def _parse_status(text: str) -> dict | None:
    m = re.search(r"DR_STATUS\s*(\{.*?\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
