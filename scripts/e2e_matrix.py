#!/usr/bin/env python
"""Full-API end-to-end matrix: every entry point x the research modes (async / cache / sync / streaming) x depths.

  python scripts/e2e_matrix.py                    # all cases in parallel (needs a running dispatcher, inflight >= 6)
  python scripts/e2e_matrix.py --only C3,C5       # subset
  python scripts/e2e_matrix.py --list             # show the cases
  python scripts/e2e_matrix.py --skip-deep        # drop the deep-depth cases (C6, C8)

Outputs e2e_out/matrix/<case>.md (reports), e2e_out/matrix/results.json and e2e_out/matrix/SUMMARY.md.
Exit code 0 only if every selected case passes.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from deepresearch.api import DeepResearchAPI  # noqa: E402
from deepresearch.config import load_settings  # noqa: E402
from deepresearch.mcp_client import GatewayClient  # noqa: E402

S = load_settings()
GW = GatewayClient(S)
API = DeepResearchAPI(S)
OUT = Path(os.environ.get("DR_E2E_OUT", ROOT / "e2e_out" / "matrix"))   # e.g. e2e_out/matrix-<stack> for a CloudFormation stack
OUT.mkdir(parents=True, exist_ok=True)
VALIDATOR = ROOT / "skills" / "deep-research-harness" / "scripts" / "validate_report.py"
TAG = time.strftime("%m%d%H%M")
TERMINAL = ("completed", "completed_no_report", "failed", "cancelled")
LOCK = threading.Lock()


def log(case: str, msg: str) -> None:
    with LOCK:
        print(f"{time.strftime('%H:%M:%S')} [{case}] {msg}", flush=True)


class CaseFailed(AssertionError):
    pass


class CaseSkipped(Exception):
    """Precondition of the case does not hold for this deployment (reported, not counted as failure)."""


def check(cond, msg):
    if not cond:
        raise CaseFailed(msg)


def validate_report(case: str, content: str) -> dict:
    p = OUT / f"{case}.md"
    p.write_text(content)
    r = subprocess.run([sys.executable, str(VALIDATOR), "--report", str(p)], capture_output=True, text=True)
    info = {"file": str(p.relative_to(ROOT)), "chars": len(content), "validator_passed": r.returncode == 0,
            "has_bibliography": "## Bibliography" in content, "has_citation": "[1]" in content}
    check(info["has_bibliography"] and info["has_citation"], f"report missing bibliography/citations: {info}")
    check(len(content) >= 3000, f"report too short ({len(content)} chars)")
    check(info["validator_passed"], f"validate_report.py failed: {r.stdout[-600:]}")
    return info


def wait_status(case: str, task_id: str, timeout: int, poll: int = 20) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        st = GW.call_tool("research___get_research_status", {"task_id": task_id})
        if st.get("status") != last:
            log(case, f"status {task_id} -> {st.get('status')} progress={st.get('progress')}")
            last = st.get("status")
        if st.get("status") in TERMINAL:
            return st
        time.sleep(poll)
    raise CaseFailed(f"{task_id} not terminal after {timeout}s")


def stream_until_done(case: str, first_tool: str, first_args: dict, timeout: int) -> tuple[dict, dict]:
    """Stream research_live / watch_research and re-attach after each ~13-min window until terminal."""
    stats = {"events": 0, "tool": 0, "reasoning": 0, "narration": 0, "first_event_s": None, "windows": 0, "task_id": None}
    t0 = time.time()

    def on_event(msg):
        if msg.get("method") != "notifications/progress":
            return
        text = msg["params"].get("message", "")
        if text.startswith("⏳"):
            stats["heartbeats"] = stats.get("heartbeats", 0) + 1
            return
        stats["events"] += 1
        stats["first_event_s"] = stats["first_event_s"] or round(time.time() - t0, 1)
        stats["tool"] += text.startswith("🔧")
        stats["reasoning"] += text.startswith("💭")
        stats["narration"] += text.startswith("💬")
        if text.startswith("submitted ") and not stats["task_id"]:
            stats["task_id"] = text.split()[1]
        if stats["events"] % 10 == 1:
            log(case, f"stream #{stats['events']}: {text[:110]!r}")

    tool, args = first_tool, first_args
    while True:
        stats["windows"] += 1
        res = GW.stream_tool(tool, args, on_event, timeout=950)
        stats["task_id"] = stats["task_id"] or res.get("task_id")
        if res.get("status") in TERMINAL or time.time() - t0 > timeout:
            return res, stats
        log(case, f"stream window {stats['windows']} ended (status={res.get('status')}); re-attaching via watch_research")
        tool, args = "live___watch_research", {"task_id": stats["task_id"] or res["task_id"]}


# --------------------------------------------------------------------------------------------- cases
def c1_gateway_websearch():
    d = GW.discover()
    check("2026-07-28" in d["supportedVersions"], d)
    tools = GW.list_tools()
    for t in ("web-search___WebSearch", "research___submit_research", "research___run_research", "research___get_research_status",
              "research___get_research_report", "research___cancel_research", "live___research", "live___research_live",
              "live___watch_research", "live___research_status", "live___cancel_research"):
        check(t in tools, f"missing tool {t}: {tools}")
    r = GW.call_tool("web-search___WebSearch", {"query": "Amazon Bedrock AgentCore harness", "maxResults": 3,
                                                  "filters": {"domainFilter": {"include": ["aws.amazon.com"]}}})
    check(r["results"] and all("aws.amazon.com" in x["url"] for x in r["results"]), r)
    return {"tools": len(tools), "results": len(r["results"]), "versions": d["supportedVersions"]}


def c2_standalone_websearch():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "websearch_mcp.py"), "--search", "AgentCore Gateway rate limiting",
                        "--max", "3", "--json"], capture_output=True, text=True, timeout=120)
    check(r.returncode == 0, r.stderr[-400:])
    data = json.loads(r.stdout)
    check(data["count"] >= 1 and data["results"][0]["url"].startswith("http"), data)
    return {"count": data["count"]}


def c3_async_cache_quick():
    """Async submit + cache: two paraphrases -> same task; after completion a third paraphrase hits the finished task."""
    q1 = "最近一周 AI 编程助手（Claude Code、Cursor、Kiro）有哪些重要更新？"
    q2 = "过去 7 天 Claude Code、Cursor 和 Kiro 这些 AI 编程工具发布了什么新功能"
    q3 = "Summarize the key updates to AI coding assistants Claude Code, Cursor and Kiro during the last week"
    a = GW.call_tool("research___submit_research", {"query": q1, "depth": "quick"})
    b = GW.call_tool("research___submit_research", {"query": q2, "depth": "quick"})
    log("C3", f"a={a['task_id']} merged={a['merged']}  b={b['task_id']} merged={b['merged']} canonical={b.get('canonical')}")
    check(b["task_id"] == a["task_id"] and b["merged"] is True, f"paraphrase did not merge: {a} / {b}")
    st = wait_status("C3", a["task_id"], 1500)
    check(st["status"] == "completed", st)
    rep = GW.call_tool("research___get_research_report", {"task_id": a["task_id"], "mode": "content"})
    info = validate_report("C3", rep["content"])
    url = GW.call_tool("research___get_research_report", {"task_id": a["task_id"], "mode": "url"})
    with urllib.request.urlopen(url["download_url"], timeout=60) as r:
        check(r.status == 200, "download_url not fetchable")
    t0 = time.time()
    c = GW.call_tool("research___submit_research", {"query": q3, "depth": "quick"})
    hit_s = round(time.time() - t0, 1)
    check(c["task_id"] == a["task_id"] and c["merged"] is True and c["status"] == "completed", f"no cache hit after completion: {c}")
    subs = API.store.list_subscriptions(a["task_id"])
    return {"task_id": a["task_id"], "first_merged": a["merged"], "subscribers": len(subs), "cache_hit_after_completion_s": hit_s,
            "searches": (st.get("progress") or {}).get("searches"), **info}


def c4_live_async_watch_standard():
    """Default MCP action (async) at standard depth, then watch_research streams (with re-attach) until completion."""
    q = "对比 2026 年主流开源 LLM 推理引擎（vLLM、SGLang、TensorRT-LLM、llama.cpp）的吞吐、显存效率、部署复杂度与适用场景"
    # cache=False: this case must always stream a fresh standard run (cache hits are covered by C3/C11)
    a = GW.call_tool("live___research", {"query": q, "depth": "standard", "cache": False})
    check(a["status"] in ("queued", "running") and a.get("task_id") and a["merged"] is False, a)
    log("C4", f"async returned in one call: {a['task_id']} merged={a['merged']} status={a['status']}")
    res, stats = stream_until_done("C4", "live___watch_research", {"task_id": a["task_id"]}, 3000)
    check(res.get("status") == "completed", res)
    check(stats["events"] >= 3, stats)
    return {"task_id": a["task_id"], **stats, **validate_report("C4", res["content"])}


def c5_sync_blocking_quick_nocache():
    """Synchronous client: ONE blocking tools/call (with a progressToken, as standard MCP clients such as Claude Code send)
    must return the finished report. Keep-alive progress every 20 s holds the connection; without a progressToken the
    gateway buffers the whole response and the ~350 s idle cut applies (use run_research + watch instead, see C5b)."""
    t0 = time.time()
    logs: list[str] = []
    r = GW.stream_tool("live___research_live", {"query": "Amazon Bedrock AgentCore Runtime V2 与 V1 的差异：冷启动、内存计费与限制",
                                                 "depth": "quick", "cache": False},
                       lambda m: logs.append(m.get("method", "")), timeout=950, progress=True)
    check(r.get("status") == "completed", f"sync call did not complete in one call: {r}")
    return {"task_id": r["task_id"], "wall_s": round(time.time() - t0), "keepalive_notifications": len(logs),
            **validate_report("C5", r["content"])}


def c5b_run_research_bounded():
    """research___run_research never blocks past the 300 s sync cap: it returns the report or status=running (+task_id),
    and the caller finishes with watch_research."""
    t0 = time.time()
    r = GW.call_tool("research___run_research", {"query": "2026 年 AWS Step Functions Distributed Map 调度 AI Agent 任务的最佳实践与限制",
                                                  "depth": "quick", "cache": False}, timeout=420)
    call_s = round(time.time() - t0)
    check(call_s <= 360, f"run_research blocked {call_s}s (> sync cap)")
    if r.get("status") != "completed":
        check(r.get("task_id") and r.get("status") in ("queued", "running"), r)
        log("C5b", f"sync cap reached after {call_s}s -> continuing with watch_research({r['task_id']})")
        r, _ = stream_until_done("C5b", "live___watch_research", {"task_id": r["task_id"]}, 1500)
    check(r.get("status") == "completed", r)
    return {"task_id": r["task_id"], "sync_call_s": call_s, "total_s": round(time.time() - t0), **validate_report("C5b", r["content"])}


def c6_research_live_deep_nocache():
    """Streaming submit+watch in one tool at deep depth, cache disabled; exercises multi-window re-attach if > 13 min."""
    q = "深度调研：2026 年 MCP（Model Context Protocol）2026-07-28 规范的无状态化改动对 Agent 平台（AgentCore、Claude Code、IDE 客户端）的影响与迁移路径"
    res, stats = stream_until_done("C6", "live___research_live", {"query": q, "depth": "deep", "cache": False}, 3300)
    check(res.get("status") == "completed", res)
    check(stats["reasoning"] + stats["narration"] >= 1, f"no thinking visible: {stats}")
    return {**stats, **validate_report("C6", res["content"])}


def c7_sdk_callback_quick():
    """Python SDK submit (cache=False) with a signed completion webhook; download the report from the webhook URL."""
    if S.state("stack_name"):
        raise CaseSkipped("dispatcher runs in ECS (CloudFormation stack): a 127.0.0.1 webhook is unreachable from it -> see C7b (SNS)")
    got: list[tuple[dict, bytes]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            got.append((dict(self.headers), body))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t = API.submit("2026 年 Amazon Bedrock 上 Claude 模型的全球跨区推理（Global CRIS）配额与限流最佳实践", depth="quick",
                       actor_id="e2e-matrix", metadata={"callback_url": f"http://127.0.0.1:{srv.server_port}/hook"}, cache=False)
        log("C7", f"submitted {t['task_id']} (SDK, cache=False, merged={t['merged']})")
        t0 = time.time()
        while not got and time.time() - t0 < 1500:
            time.sleep(10)
        check(got, f"webhook not received; task={API.get(t['task_id'])}")
        headers, body = got[0]
        payload = json.loads(body)
        sig = {k.lower(): v for k, v in headers.items()}.get("x-nx-signature", "")
        want = "sha256=" + hmac.new(S.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        check(hmac.compare_digest(sig, want), "bad webhook signature")
        check(payload["status"] == "completed", payload)
        with urllib.request.urlopen(payload["download_url"], timeout=60) as r:
            content = r.read().decode()
        return {"task_id": t["task_id"], "webhook_event": payload["event"], **validate_report("C7", content)}
    finally:
        srv.shutdown()


def c7b_sns_completion_quick():
    """Completion notification over SNS (works with a remote dispatcher): temp SQS queue subscribed to the task topic."""
    import boto3
    topic = S.state("sns_topic_arn")
    check(topic, "sns_topic_arn missing in deploy state")
    sqs, sns = boto3.client("sqs", region_name=S.queue_region), boto3.client("sns", region_name=S.queue_region)
    qurl = sqs.create_queue(QueueName=f"{S.project}-e2e-sns-{TAG}"[:80])["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.set_queue_attributes(QueueUrl=qurl, Attributes={"Policy": json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "sns.amazonaws.com"}, "Action": "sqs:SendMessage", "Resource": qarn,
        "Condition": {"ArnEquals": {"aws:SourceArn": topic}}}]})})
    sub = sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn, ReturnSubscriptionArn=True)["SubscriptionArn"]
    try:
        t = API.submit(f"2026 年 AWS 上 MCP 服务器托管方案对比：AgentCore Runtime、Lambda、ECS（run {TAG}）", depth="quick",
                       actor_id="e2e-matrix", cache=False)
        log("C7b", f"submitted {t['task_id']} (SDK, cache=False); waiting for the SNS completion message")
        t0, payload = time.time(), None
        while payload is None and time.time() - t0 < 1500:
            for m in sqs.receive_message(QueueUrl=qurl, WaitTimeSeconds=20, MaxNumberOfMessages=10).get("Messages", []):
                msg = json.loads(json.loads(m["Body"])["Message"])
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=m["ReceiptHandle"])
                if msg.get("task_id") == t["task_id"]:
                    payload = msg
        check(payload, f"no SNS notification; task={API.get(t['task_id'])}")
        check(payload["status"] == "completed", payload)
        with urllib.request.urlopen(payload["download_url"], timeout=60) as r:
            content = r.read().decode()
        return {"task_id": t["task_id"], "sns_event": payload["event"], "notify_s": round(time.time() - t0), **validate_report("C7b", content)}
    finally:
        sns.unsubscribe(SubscriptionArn=sub)
        sqs.delete_queue(QueueUrl=qurl)


def c8_cancel_force_deep():
    """Start a deep task (cache=False), wait until running, force-cancel -> cancelled; backend session stopped."""
    a = GW.call_tool("research___submit_research", {"query": f"全面调研 2026 年全球半导体先进封装产业链（run {TAG}）", "depth": "deep", "cache": False})
    tid = a["task_id"]
    t0 = time.time()
    while time.time() - t0 < 600 and (API.get(tid) or {}).get("status") != "running":
        time.sleep(5)
    check((API.get(tid) or {}).get("status") == "running", f"never started running: {API.get(tid)}")
    time.sleep(30)
    c = GW.call_tool("live___cancel_research", {"task_id": tid, "force": True})
    check(c["status"] == "cancelled", c)
    time.sleep(15)
    final = API.get(tid)
    check(final["status"] == "cancelled", final)
    return {"task_id": tid, "session_stopped": c.get("session_stopped"), "previous_status": c.get("previous_status")}


def c9_merged_detach_semantics():
    """Two paraphrases share a task; detaching one subscriber keeps the backend alive; force stops it."""
    x = f"Zephyr{TAG}"
    a = GW.call_tool("research___submit_research", {"query": f"调研最近1天关于 {x} 编译器的新闻", "depth": "quick"})
    b = GW.call_tool("live___research", {"query": f"给出昨天 {x} 编译器的新闻", "depth": "quick"})
    check(b["task_id"] == a["task_id"] and b["merged"], (a, b))
    d = GW.call_tool("research___cancel_research", {"task_id": a["task_id"], "subscription_id": b["subscription_id"]})
    check(d.get("remaining_subscribers") == 1 and d.get("status") != "cancelled", d)
    refused = GW.call_tool("research___cancel_research", {"task_id": a["task_id"]})   # only 1 subscriber left -> allowed to stop
    check(refused.get("status") == "cancelled", refused)
    return {"task_id": a["task_id"], "detach": d, "final": refused.get("status")}


def c10_cache_false_not_registered():
    x = f"Quasar{TAG}"
    a = GW.call_tool("live___research", {"query": f"最近一周 {x} 数据库的重要发布", "depth": "quick", "cache": False})
    b = GW.call_tool("live___research", {"query": f"最近一周 {x} 数据库的重要发布", "depth": "quick"})
    check(a["merged"] is False and b["merged"] is False and a["task_id"] != b["task_id"], (a, b))
    c = GW.call_tool("research___submit_research", {"query": f"过去 7 天 {x} 数据库发布了什么", "depth": "quick"})
    check(c["task_id"] == b["task_id"] and c["merged"], f"cached path should merge with b: {c}")
    for tid in {a["task_id"], b["task_id"]}:
        GW.call_tool("live___cancel_research", {"task_id": tid, "force": True})
    return {"cache_false_task": a["task_id"], "cached_task": b["task_id"], "merged_into_cached": c["task_id"] == b["task_id"]}


def c11_cache_hit_completed_standard():
    """Cache on: a paraphrase of an already-completed standard research returns the finished task instantly."""
    q = "Milvus、Qdrant、Weaviate 和 pgvector 这几个开源向量库在 2026 年谁更适合什么场景？请比较性能与运维难度"
    t0 = time.time()
    a = GW.call_tool("live___research", {"query": q, "depth": "standard"})
    hit_s = round(time.time() - t0, 1)
    check(a["merged"] is True and a["status"] == "completed", f"expected a cache hit on the completed vector-DB task: {a}")
    res = GW.call_tool("live___watch_research", {"task_id": a["task_id"]}, timeout=120)   # terminal -> returns report at once
    check(res.get("status") == "completed", res)
    return {"task_id": a["task_id"], "reason": a.get("reason"), "cache_hit_s": hit_s, **validate_report("C11", res["content"])}


CASES = {
    "C1": ("Gateway 直连：discover + tools/list + WebSearch(域名过滤)", "—", "sync", c1_gateway_websearch),
    "C2": ("独立 WebSearch MCP 脚本 (CLI)", "—", "sync", c2_standalone_websearch),
    "C3": ("research___submit_research 异步 + 缓存合并 + 完成后命中", "quick", "async+cache", c3_async_cache_quick),
    "C4": ("live___research 异步(默认) + watch_research 流式", "standard", "async+stream, cache=false", c4_live_async_watch_standard),
    "C5": ("live___research_live 同步单次阻塞调用（单窗口内完成）", "quick", "sync, cache=false", c5_sync_blocking_quick_nocache),
    "C5b": ("research___run_research 限时同步(≤300s)+watch 接续", "quick", "sync-bounded, cache=false", c5b_run_research_bounded),
    "C6": ("live___research_live 提交+流式(多窗口重挂)", "deep", "stream, cache=false", c6_research_live_deep_nocache),
    "C7": ("Python SDK submit + 签名 webhook 回调", "quick", "async+callback, cache=false", c7_sdk_callback_quick),
    "C7b": ("SNS 完成通知（远端 dispatcher 亦可）", "quick", "async+SNS, cache=false", c7b_sns_completion_quick),
    "C8": ("强制取消运行中的 deep 任务", "deep", "cancel force", c8_cancel_force_deep),
    "C9": ("合并任务：撤销单个订阅不停后端", "quick", "cache merge + detach", c9_merged_detach_semantics),
    "C10": ("cache=false 不写缓存，后续缓存请求不合并到它", "quick", "cache on/off", c10_cache_false_not_registered),
    "C11": ("缓存命中已完成的 standard 任务，立即返回报告", "standard", "async+cache hit", c11_cache_hit_completed_standard),
}


def run_case(cid: str) -> dict:
    title, depth, mode, fn = CASES[cid]
    log(cid, f"START {title} depth={depth} mode={mode}")
    t0 = time.time()
    try:
        detail = fn()
        ok, err = True, None
    except CaseSkipped as e:
        ok, err, detail = None, f"skipped: {e}", {}
    except Exception as e:  # noqa: BLE001
        ok, err, detail = False, f"{type(e).__name__}: {e}"[:800], {"traceback": traceback.format_exc()[-1500:]}
    res = {"case": cid, "title": title, "depth": depth, "mode": mode, "passed": ok, "seconds": round(time.time() - t0), "error": err, "detail": detail}
    log(cid, f"{'PASS' if ok else 'SKIP' if ok is None else 'FAIL'} in {res['seconds']}s {err or ''}")
    with LOCK:
        results = json.loads((OUT / "results.json").read_text()) if (OUT / "results.json").exists() else {}
        results[cid] = res
        (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--skip-deep", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--parallel", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="merge into existing results.json (re-running a subset)")
    a = ap.parse_args()
    if a.list:
        for k, (t, d, m, _) in CASES.items():
            print(f"{k:4} depth={d:<9} mode={m:<28} {t}")
        return 0
    ids = a.only.split(",") if a.only else list(CASES)
    if a.skip_deep:
        ids = [i for i in ids if CASES[i][1] != "deep"]
    if not a.keep:
        (OUT / "results.json").unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        results = list(ex.map(run_case, ids))
    if a.keep:  # summary over everything recorded so far, in case order
        allres = json.loads((OUT / "results.json").read_text())
        results = [allres[k] for k in CASES if k in allres]
    lines = ["| 用例 | 场景 | 深度 | 模式 | 结果 | 耗时(s) | 关键数据 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        d = r["detail"] if r["passed"] else {"error": r["error"]}
        keys = ("task_id", "chars", "validator_passed", "events", "windows", "reasoning", "narration", "subscribers",
                "cache_hit_after_completion_s", "cache_hit_s", "wall_s", "sync_call_s", "total_s", "keepalive_notifications",
                "searches", "session_stopped", "tools", "results", "count", "error")
        brief = ", ".join(f"{k}={d[k]}" for k in keys if k in d and d[k] not in (None, ""))
        lines.append(f"| {r['case']} | {r['title']} | {r['depth']} | {r['mode']} | {'✅' if r['passed'] else '⏭️' if r['passed'] is None else '❌'} | {r['seconds']} | {brief[:220]} |")
    summary = "\n".join(lines)
    (OUT / "SUMMARY.md").write_text(f"# E2E matrix {time.strftime('%Y-%m-%d %H:%M')}\n\n{summary}\n")
    print("\n" + summary)
    failed = [r["case"] for r in results if r["passed"] is False]
    skipped = [r["case"] for r in results if r["passed"] is None]
    print(f"\npassed={len(results) - len(failed) - len(skipped)} failed={len(failed)} {failed or ''} skipped={skipped or 0}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
