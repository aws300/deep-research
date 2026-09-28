"""InvokeHarness streaming client + InvokeAgentRuntimeCommand helper.

Normalised events yielded by `invoke()`:
  {"type": "text", "text": str}
  {"type": "reasoning", "text": str}
  {"type": "tool_use", "name": str, "tool_use_id": str, "input": str}
  {"type": "tool_result", "tool_use_id": str, "content": Any}
  {"type": "stop", "reason": str}
  {"type": "usage", "input_tokens": int, "output_tokens": int, "cache_read": int, "latency_ms": int}
  {"type": "error", "kind": str, "message": str}
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from .aws_clients import client
from .config import Settings


def new_session_id() -> str:
    """runtimeSessionId must be >= 33 chars."""
    return f"dr-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"


@dataclass
class InvokeResult:
    text: str = ""
    stop_reason: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    events: int = 0


class HarnessClient:
    def __init__(self, settings: Settings, harness_arn: str | None = None):
        self.s = settings
        self.arn = harness_arn or settings.state("harness_arn")
        if not self.arn:
            raise RuntimeError("harness_arn missing: deploy first or pass harness_arn")
        self.dp = client("bedrock-agentcore", settings.harness_region)                       # long read: event streams
        self.cmd = client("bedrock-agentcore", settings.harness_region, long_read=False)     # short read: shell commands

    # ------------------------------------------------------------------ invoke
    def invoke(self, prompt: str, session_id: str, *, actor_id: str | None = None,
               overrides: dict | None = None) -> Iterator[dict]:
        req: dict[str, Any] = {
            "harnessArn": self.arn,
            "runtimeSessionId": session_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
        }
        if actor_id:
            req["actorId"] = actor_id
        if overrides:
            req.update(overrides)
        resp = self.dp.invoke_harness(**req)
        yield from parse_stream(resp["stream"])

    def invoke_collect(self, prompt: str, session_id: str, **kw) -> InvokeResult:
        r = InvokeResult()
        pending: dict[str, dict] = {}
        for ev in self.invoke(prompt, session_id, **kw):
            r.events += 1
            t = ev["type"]
            if t == "text":
                r.text += ev["text"]
            elif t == "tool_use":
                pending[ev["tool_use_id"]] = ev
                r.tool_calls.append(ev)
            elif t == "stop":
                r.stop_reason = ev["reason"]
            elif t == "usage":
                r.usage = ev
            elif t == "error":
                raise RuntimeError(f"{ev['kind']}: {ev['message']}")
        return r

    # ----------------------------------------------------------------- command
    def run_command(self, session_id: str, command: str, timeout: int = 120, wall_timeout: float | None = None) -> tuple[int, str]:
        """Run a shell command inside the session microVM (no model involved). Returns (exit_code, stdout).
        A hard wall-clock limit (default timeout+30 s) guards against command streams that never end."""
        import concurrent.futures as cf
        ex = cf.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._run_command, session_id, command, timeout)
        try:
            return fut.result(timeout=wall_timeout or timeout + 30)
        except cf.TimeoutError as e:
            raise TimeoutError(f"InvokeAgentRuntimeCommand exceeded {wall_timeout or timeout + 30}s (session {session_id})") from e
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _run_command(self, session_id: str, command: str, timeout: int) -> tuple[int, str]:
        resp = self.cmd.invoke_agent_runtime_command(
            agentRuntimeArn=self.arn, runtimeSessionId=session_id,
            body={"command": command, "timeout": timeout})
        out, code = [], 0
        for raw in resp["stream"]:
            chunk = raw.get("chunk", raw)  # events arrive wrapped as {"chunk": {...}}
            if "contentDelta" in chunk:
                d = chunk["contentDelta"]
                out.append(d.get("stdout") or d.get("text") or d.get("stderr") or "")
            elif "contentStop" in chunk:
                code = int(chunk["contentStop"].get("exitCode", 0))
            elif "contentStart" in chunk:
                continue
            else:  # error events
                for k, v in chunk.items():
                    if isinstance(v, dict) and "message" in v:
                        raise RuntimeError(f"{k}: {v['message']}")
        return code, "".join(out)

    def read_file(self, session_id: str, path: str) -> str | None:
        code, out = self.run_command(session_id, f"test -f {path} && cat {path}", timeout=60)
        return out if code == 0 else None


def parse_stream(stream) -> Iterator[dict]:
    """Translate raw InvokeHarness event-stream into normalised events (see module docstring)."""
    blocks: dict[int, dict] = {}
    for ev in stream:
        if "contentBlockStart" in ev:
            cb = ev["contentBlockStart"]
            start = cb.get("start", {})
            if "toolUse" in start:
                blocks[cb["contentBlockIndex"]] = {"kind": "toolUse", "name": start["toolUse"].get("name"),
                                                   "id": start["toolUse"].get("toolUseId"), "input": ""}
            elif "toolResult" in start:
                blocks[cb["contentBlockIndex"]] = {"kind": "toolResult", "id": start["toolResult"].get("toolUseId"),
                                                   "content": []}
        elif "contentBlockDelta" in ev:
            cb = ev["contentBlockDelta"]
            d = cb.get("delta", {})
            if "text" in d:
                yield {"type": "text", "text": d["text"]}
            elif "reasoningContent" in d:
                rc = d["reasoningContent"]
                txt = rc.get("text") or (rc.get("reasoningText") or {}).get("text") or ""
                if txt:
                    yield {"type": "reasoning", "text": txt}
            elif "toolUse" in d:
                b = blocks.setdefault(cb["contentBlockIndex"], {"kind": "toolUse", "name": None, "id": None, "input": ""})
                b["input"] += d["toolUse"].get("input", "")
            elif "toolResult" in d:
                b = blocks.setdefault(cb["contentBlockIndex"], {"kind": "toolResult", "id": None, "content": []})
                b["content"].extend(d["toolResult"] if isinstance(d["toolResult"], list) else [d["toolResult"]])
        elif "contentBlockStop" in ev:
            idx = ev["contentBlockStop"]["contentBlockIndex"]
            b = blocks.pop(idx, None)
            if b and b["kind"] == "toolUse":
                yield {"type": "tool_use", "name": b["name"], "tool_use_id": b["id"], "input": b["input"]}
            elif b and b["kind"] == "toolResult":
                yield {"type": "tool_result", "tool_use_id": b["id"], "content": b["content"]}
        elif "messageStop" in ev:
            yield {"type": "stop", "reason": ev["messageStop"].get("stopReason", "")}
        elif "metadata" in ev:
            u = ev["metadata"].get("usage", {})
            yield {"type": "usage", "input_tokens": u.get("inputTokens", 0), "output_tokens": u.get("outputTokens", 0),
                   "cache_read": u.get("cacheReadInputTokens", 0),
                   "latency_ms": ev["metadata"].get("metrics", {}).get("latencyMs", 0)}
        elif "messageStart" in ev:
            continue
        else:
            for k, v in ev.items():
                if isinstance(v, dict) and "message" in v:
                    yield {"type": "error", "kind": k, "message": v["message"]}


def summarize_tool_input(raw: str, limit: int = 160) -> str:
    try:
        obj = json.loads(raw) if raw else {}
        q = obj.get("query") or obj.get("command") or obj.get("path") or json.dumps(obj)[:limit]
        return str(q)[:limit]
    except Exception:
        return raw[:limit]
