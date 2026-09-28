"""End-to-end tests against the deployed stack. Run:  DR_E2E=1 pytest tests/e2e -m e2e -s --timeout=2400

Order matters (cheap -> expensive): gateway tools/list -> WebSearch call -> harness smoke -> full pipeline.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import boto3
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from deepresearch.api import DeepResearchAPI
from deepresearch.config import load_settings
from deepresearch.dispatcher import Dispatcher
from deepresearch.harness_client import HarnessClient, new_session_id
from deepresearch.task_store import TaskStore

pytestmark = pytest.mark.e2e
S = load_settings()
MCP_PROFILE = os.environ.get("DR_MCP_PROFILE") or S.raw.get("mcp_client", {}).get("aws_profile", "")


META_2026 = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
             "io.modelcontextprotocol/clientInfo": {"name": "nx-e2e", "version": "0.1"},
             "io.modelcontextprotocol/clientCapabilities": {}}


def _mcp(url: str, payload: dict) -> dict:
    """JSON-RPC over streamable HTTP to the Gateway with SigV4, MCP 2026-07-28 stateless style
    (no initialize, version/client info in _meta, Mcp-Method/Mcp-Name headers, no Mcp-Session-Id)."""
    payload = dict(payload)
    payload["params"] = {"_meta": META_2026, **payload.get("params", {})}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": payload["method"]}
    if payload["method"] == "tools/call":
        headers["Mcp-Name"] = payload["params"]["name"]
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(boto3.Session().get_credentials(), "bedrock-agentcore", S.gateway_region).add_auth(req)
    r = urllib.request.Request(url, data=body, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read().decode()
    if raw.lstrip().startswith(("event:", "data:")) or "\ndata:" in raw:  # SSE framing (gateway streaming enabled)
        raw = "".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(raw)


@pytest.fixture(scope="session")
def gateway_url():
    url = S.state("gateway_url")
    assert url, "deploy first (config/deploy_state.json missing gateway_url)"
    return url


@pytest.fixture(scope="session")
def websearch_tool(gateway_url):
    r = _mcp(gateway_url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [t["name"] for t in r["result"]["tools"]]
    ws = [n for n in names if n.endswith("WebSearch")]
    assert ws, f"WebSearch tool not exposed by gateway; tools={names}"
    return ws[0]


def test_gateway_lists_websearch(websearch_tool):
    assert "web-search" in websearch_tool


def test_gateway_websearch_returns_cited_results(gateway_url, websearch_tool):
    r = _mcp(gateway_url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": websearch_tool,
                                      "arguments": {"query": "Amazon Bedrock AgentCore Runtime V2 platform version", "maxResults": 5}}})
    assert "result" in r, r
    content = r["result"]["content"][0]["text"]
    data = json.loads(content)
    assert data["results"], data
    first = data["results"][0]
    assert first["url"].startswith("http") and first["text"]


def test_harness_smoke_invocation():
    hc = HarnessClient(S)
    sid = new_session_id()
    res = hc.invoke_collect("Reply with exactly the word PONG and nothing else. Do not use tools.", sid,
                            overrides={"maxIterations": 3, "allowedTools": ["none-such-tool"]})
    assert res.stop_reason in ("end_turn", "max_tokens"), res
    assert "PONG" in res.text.upper()
    assert res.usage.get("input_tokens", 0) > 0


def test_harness_can_search_and_cite():
    hc = HarnessClient(S)
    sid = new_session_id()
    res = hc.invoke_collect(
        "Use the WebSearch tool once with query 'AgentCore harness generally available June 2026' and maxResults 3, "
        "then answer in one sentence with the URL of the best result.", sid, overrides={"maxIterations": 6})
    assert any(tc["name"].endswith("WebSearch") for tc in res.tool_calls), res.tool_calls
    assert "http" in res.text


def test_harness_shell_and_skill_bootstrap():
    """Scripts are installed into the microVM via the command API before the first model turn."""
    from deepresearch.worker import ResearchWorker
    hc = HarnessClient(S)
    sid = new_session_id()
    tools_dir = ResearchWorker(S, harness=hc).bootstrap_session(sid)
    code, out = hc.run_command(sid, f"python3 --version && python3 {tools_dir}/validate_report.py --help | head -3")
    assert code == 0, out
    assert "Python 3" in out and "report" in out, out


@pytest.mark.timeout(2400)
def test_full_pipeline_quick_research():
    """submit -> SQS -> dispatcher admission -> harness research -> report in S3 -> validated structure."""
    api = DeepResearchAPI(S)
    store = TaskStore(S)
    store.reset_slots()
    query = os.environ.get("DR_E2E_QUERY", f"Summarize what changed in Amazon Bedrock AgentCore Runtime V2 (September 2026) versus V1, with sources. [run {int(time.time())}]")
    task = api.submit(query, depth="quick", actor_id="e2e", dedupe=False)
    d = Dispatcher(S)
    d.admission.set_max_inflight(2)
    dispatched = 0
    for _ in range(12):
        dispatched += d.run_once(wait=5)
        if dispatched:
            break
        # a separately running dispatcher (scripts/run_dispatcher.py) may have claimed the task already
        if (api.get(task["task_id"]) or {}).get("status") in ("running", "completed", "completed_no_report"):
            break
    if not dispatched:
        assert (api.get(task["task_id"]) or {}).get("status") != "queued", "task was not dispatched (check queue/admission)"
    t0 = time.time()
    final = None
    while time.time() - t0 < 2100:
        t = api.get(task["task_id"])
        if t["status"] in ("completed", "completed_no_report", "failed"):
            final = t
            break
        time.sleep(15)
    d.stop()
    assert final, "task did not finish in time"
    assert final["status"] == "completed", final.get("last_error") or final.get("result")
    events = api.events(task["task_id"])
    kinds = {e.get("kind") for e in events}
    assert "tool_use" in kinds, "no tool_use progress events were recorded"
    assert any(e.get("kind") == "tool_use" and str(e.get("tool", "")).endswith("WebSearch") for e in events)
    report = api.report(task["task_id"])
    assert report and "## Bibliography" in report and "## Executive Summary" in report
    assert "[1]" in report
    assert store.inflight() == 0, "in-flight slot was not released"


# ---------------------------------------------------------------- MCP 2026-07-28 specifics
def test_gateway_mcp_2026_07_28_server_discover(gateway_url):
    r = _mcp(gateway_url, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
    # internal gateway also keeps 2025-11-25 for the harness runtime; the client-facing gateway is 2026-07-28 only
    assert "2026-07-28" in r["result"]["supportedVersions"], r
    assert r["result"]["serverInfo"]["name"]


def test_gateway_rejects_legacy_protocol_version(gateway_url):
    """Only 2026-07-28 is enabled: a 2025-06-18 client must get UnsupportedProtocolVersion."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    req = AWSRequest(method="POST", url=gateway_url, data=body,
                     headers={"Content-Type": "application/json", "Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})
    SigV4Auth(boto3.Session().get_credentials(), "bedrock-agentcore", S.gateway_region).add_auth(req)
    r = urllib.request.Request(gateway_url, data=body, headers=dict(req.headers), method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read().decode(); code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(); code = e.code
    assert code == 400 and "Unsupported protocol version" in raw, (code, raw[:300])


# ---------------------------------------------------------------- least-privilege IAM key (MCP clients)
def test_gateway_accepts_least_privilege_iam_key(gateway_url, websearch_tool):
    """The MCP client profile (least-privilege IAM, InvokeGateway only) can call WebSearch over MCP 2026-07-28."""
    try:
        creds = boto3.Session(profile_name=MCP_PROFILE).get_credentials()
    except Exception:  # noqa: BLE001
        pytest.skip(f"AWS profile {MCP_PROFILE!r} not configured (python scripts/mcp_connect.py --setup-profile)")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"_meta": META_2026, "name": websearch_tool, "arguments": {"query": "least privilege IAM", "maxResults": 1}}}
    body = json.dumps(payload).encode()
    req = AWSRequest(method="POST", url=gateway_url, data=body,
                     headers={"Content-Type": "application/json", "Accept": "application/json", "MCP-Protocol-Version": "2026-07-28",
                              "Mcp-Method": "tools/call", "Mcp-Name": websearch_tool})
    SigV4Auth(creds, "bedrock-agentcore", S.gateway_region).add_auth(req)
    r = urllib.request.Request(gateway_url, data=body, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        out = json.loads(resp.read().decode())
    assert out["result"]["isError"] is False


# ---------------------------------------------------------------- research pipeline exposed as MCP tools (Lambda target)
def _mcp_iam(url: str, method: str, params: dict, name: str | None = None, timeout: int = 90) -> dict:
    """2026-07-28 stateless call signed with the least-privilege MCP client profile (falls back to default credentials)."""
    try:
        creds = boto3.Session(profile_name=MCP_PROFILE).get_credentials()
    except Exception:  # noqa: BLE001
        creds = boto3.Session().get_credentials()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": META_2026, **params}}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", S.gateway_region).add_auth(req)
    r = urllib.request.Request(url, data=body, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _tool_json(resp: dict) -> dict:
    assert "result" in resp, resp
    assert resp["result"].get("isError") is not True, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_gateway_exposes_research_tools(gateway_url):
    names = [t["name"] for t in _mcp_iam(gateway_url, "tools/list", {})["result"]["tools"]]
    for tool in ("research___submit_research", "research___get_research_status", "research___get_research_report"):
        assert tool in names, names


@pytest.mark.timeout(1800)
def test_mcp_research_tools_end_to_end(gateway_url):
    """submit_research -> (dispatcher executes on the harness) -> get_research_status -> get_research_report (Markdown on S3).
    Requires a running dispatcher: python scripts/run_dispatcher.py"""
    query = os.environ.get("DR_E2E_QUERY", f"What changed in Amazon Bedrock AgentCore Runtime V2 (Sept 2026) vs V1? Cite sources. [run {int(time.time())}]")
    sub = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research",
                                                          "arguments": {"query": query, "depth": "quick"}},
                              name="research___submit_research", timeout=120))
    assert sub.get("task_id") and sub["status"] in ("queued", "running", "completed"), sub
    tid = sub["task_id"]
    t0, st = time.time(), {}
    while time.time() - t0 < 1500:
        st = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___get_research_status", "arguments": {"task_id": tid}},
                                 name="research___get_research_status"))
        if st["status"] in ("completed", "completed_no_report", "failed"):
            break
        time.sleep(20)
    assert st["status"] == "completed", st
    assert st["report_s3"].startswith("s3://"), st
    assert any(step["step"] == "tool_use" for step in st["recent_steps"]), st
    rep = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___get_research_report", "arguments": {"task_id": tid, "mode": "content"}},
                              name="research___get_research_report"))
    assert rep["format"] == "markdown" and "## Bibliography" in rep["content"] and "[1]" in rep["content"]
    url = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___get_research_report", "arguments": {"task_id": tid, "mode": "url"}},
                              name="research___get_research_report"))
    assert url["download_url"].startswith("https://") and url["report_s3"] == st["report_s3"]
    with urllib.request.urlopen(url["download_url"], timeout=60) as resp:
        assert resp.status == 200



# ---------------------------------------------------------------- no-polling paths: synchronous run_research + completion callbacks
@pytest.mark.timeout(1000)
def test_run_research_sync_returns_report_in_one_call(gateway_url):
    """One MCP tool call blocks server-side and returns the finished Markdown report (quick depth). Needs a running dispatcher."""
    t0 = time.time()
    rep = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___run_research",
                                                          "arguments": {"query": os.environ.get("DR_E2E_QUERY", f"AgentCore Runtime V2 vs V1: what changed in September 2026? Cite sources. [run {int(time.time())}]"),
                                                                        "depth": "quick", "cache": False}},
                              name="research___run_research", timeout=420))  # bounded: returns within the 300 s sync cap
    assert time.time() - t0 <= 360, "run_research exceeded its sync cap"
    if rep.get("status") != "completed":   # graceful fallback -> finish via the streaming watcher
        assert rep.get("task_id") and "watch_research" in rep.get("note", ""), rep
        final = None
        for _ in range(4):
            final = _sse_call(gateway_url, "live___watch_research", {"task_id": rep["task_id"]}, lambda m: None, timeout=900)
            res = json.loads(final["result"]["content"][0]["text"])
            if res.get("status") == "completed":
                break
        rep = res
    assert rep.get("status") == "completed", rep
    assert rep["format"] == "markdown" and "## Bibliography" in rep["content"], rep.get("note")
    assert rep["download_url"].startswith("https://") and rep["report_s3"].startswith("s3://")


@pytest.mark.timeout(1200)
def test_completion_callbacks_webhook_and_sns():
    """submit_research(callback_url) -> worker POSTs a signed webhook on completion, and publishes to the SNS topic
    (verified through a temporary SQS subscription). Needs a running dispatcher on this machine (loopback webhook)."""
    import hashlib
    import hmac
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[tuple[dict, dict]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((dict(self.headers), json.loads(body)))
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):  # silence
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cb = f"http://127.0.0.1:{srv.server_port}/hook"

    # temporary SQS subscription to the completion topic
    region = S.queue_region
    sqs, sns = boto3.client("sqs", region_name=region), boto3.client("sns", region_name=region)
    topic = S.state("sns_topic_arn"); assert topic, "deploy --only queues first"
    qurl = sqs.create_queue(QueueName=f"{S.project}-e2e-{int(time.time())}")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.set_queue_attributes(QueueUrl=qurl, Attributes={"Policy": json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "sns.amazonaws.com"}, "Action": "sqs:SendMessage", "Resource": qarn,
        "Condition": {"ArnEquals": {"aws:SourceArn": topic}}}]})})
    sub = sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn, ReturnSubscriptionArn=True)["SubscriptionArn"]
    try:
        api = DeepResearchAPI(S)
        task = api.submit(f"Summarize AgentCore harness GA (June 2026) in 5 bullet points with sources. [run {int(time.time())}]", depth="quick",
                          actor_id="e2e-callback", metadata={"callback_url": cb}, dedupe=False)
        t0 = time.time()
        while time.time() - t0 < 1000 and not received:
            time.sleep(10)
        assert received, f"webhook not received; task={api.get(task['task_id'])}"
        headers, payload = received[0]
        assert payload["task_id"] == task["task_id"] and payload["event"] == "research.task.completed"
        assert payload["report_s3"].startswith("s3://") and payload["download_url"].startswith("https://")
        secret = S.webhook_secret
        sig = {k.lower(): v for k, v in headers.items()}["x-nx-signature"]
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        assert hmac.compare_digest(sig, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        # SNS copy
        got = None
        for _ in range(12):
            msgs = sqs.receive_message(QueueUrl=qurl, MaxNumberOfMessages=10, WaitTimeSeconds=5).get("Messages", [])
            for m in msgs:
                inner = json.loads(json.loads(m["Body"])["Message"])
                if inner["task_id"] == task["task_id"]:
                    got = inner
            if got:
                break
        assert got and got["status"] == "completed", "SNS notification not received"
    finally:
        sns.unsubscribe(SubscriptionArn=sub); sqs.delete_queue(QueueUrl=qurl); srv.shutdown()


# ---------------------------------------------------------------- live streaming (Runtime-hosted MCP server behind the Gateway)
def _sse_call(url: str, name: str, arguments: dict, on_event, timeout: int = 900) -> dict:
    """tools/call over streamable HTTP with SSE, SigV4 (MCP client profile). Streams notifications to on_event, returns final result."""
    try:
        creds = boto3.Session(profile_name=MCP_PROFILE).get_credentials()
    except Exception:  # noqa: BLE001
        creds = boto3.Session().get_credentials()
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"_meta": {**META_2026, "progressToken": "e2e-progress-1"}, "name": name, "arguments": arguments}}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json",
               "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": name}
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", S.gateway_region).add_auth(req)
    r = urllib.request.Request(url, data=body, headers=dict(req.headers), method="POST")
    final = None
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" not in ctype:
            return json.loads(resp.read().decode())
        buf = []
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "" and buf:
                msg = json.loads("".join(buf)); buf = []
                if "method" in msg:
                    on_event(msg)
                elif "result" in msg or "error" in msg:
                    final = msg
                    break
    return final


def test_live_tools_listed(gateway_url):
    names = [t["name"] for t in _mcp_iam(gateway_url, "tools/list", {})["result"]["tools"]]
    for tool in ("live___research", "live___watch_research", "live___cancel_research"):
        assert tool in names, names


@pytest.mark.timeout(1000)
def test_live_research_streams_progress_then_returns_report(gateway_url):
    """Default interactive mode: one call streams step notifications and ends with the Markdown report."""
    events: list[dict] = []
    final = _sse_call(gateway_url, "live___research_live",
                      {"query": os.environ.get("DR_E2E_QUERY", f"AgentCore Runtime V2 vs V1 (Sept 2026): key changes, with sources. [run {int(time.time())}]"), "depth": "quick"},
                      events.append, timeout=900)
    progress = [e for e in events if e.get("method") == "notifications/progress"]
    logs = [e for e in events if e.get("method") == "notifications/message"]
    assert progress or logs, f"no streamed notifications; got {events[:3]}"
    texts = " ".join(json.dumps(e, ensure_ascii=False) for e in events)
    assert "WebSearch" in texts or "submitted" in texts
    res = json.loads(final["result"]["content"][0]["text"])
    assert res.get("status") == "completed", res
    assert "## Bibliography" in res["content"] and res["report_s3"].startswith("s3://")


@pytest.mark.timeout(600)
def test_live_cancel_stops_running_task(gateway_url):
    """Start a research, cancel it via cancel_research after the first steps, verify the task ends as cancelled."""
    import threading
    events: list[dict] = []
    holder: dict = {}

    def run():
        try:
            holder["final"] = _sse_call(gateway_url, "live___research_live", {"query": f"History of the Model Context Protocol, with sources. [run {int(time.time())}]", "depth": "quick"},
                                        events.append, timeout=900)
        except Exception as e:  # noqa: BLE001
            holder["error"] = str(e)

    th = threading.Thread(target=run, daemon=True); th.start()
    tid = None
    t0 = time.time()
    while time.time() - t0 < 240 and not tid:
        for e in events:
            m = re.search(r"submitted (t\d+-[0-9a-f]+)", json.dumps(e))
            if m:
                tid = m.group(1); break
        time.sleep(2)
    assert tid, "did not observe task submission in the stream"
    # wait until the task is actually running (dispatcher claimed it)
    api = DeepResearchAPI(S)
    t1 = time.time()
    while time.time() - t1 < 240 and (api.get(tid) or {}).get("status") != "running":
        time.sleep(5)
    res = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___cancel_research", "arguments": {"task_id": tid, "force": True}}, name="live___cancel_research"))
    assert res["status"] == "cancelled", res
    th.join(timeout=120)
    t = api.get(tid)
    assert t["status"] == "cancelled", t
    assert TaskStore(S).inflight() >= 0


# ---------------------------------------------------------------- terminal live client (thinking visible in real time)
@pytest.mark.timeout(1000)
def test_live_research_cli_shows_thinking_in_real_time():
    """scripts/live_research.py --assert: events stream within 90s, reasoning/narration steps appear, report returned."""
    import subprocess
    script = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "live_research.py")
    r = subprocess.run([sys.executable, script, os.environ.get("DR_E2E_QUERY", "AgentCore Gateway Web Search connector: capabilities, pricing, regions (2026)."),
                        "--depth", "quick", "--assert", "--first-event-timeout", "90", "--save", "e2e_out/report_cli.md"],
                       capture_output=True, text=True, timeout=950)
    print(r.stdout[-3000:])
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-800:]
    assert "💭" in r.stdout or "💬" in r.stdout, "no thinking/narration lines were streamed"


# ---------------------------------------------------------------- semantic merge (dedupe) + subscription cancel semantics
@pytest.mark.timeout(300)
def test_semantic_merge_same_task_id_via_mcp_tools(gateway_url):
    """Two differently worded but equivalent async requests -> same task_id (merged=true), a different topic -> new task."""
    from deepresearch.api import DeepResearchAPI
    tag = int(time.time()) % 100000  # unique subject so this test never merges with older cache entries
    q1, q2, q3 = (f"调研最近1天关于 Agent{tag} 框架的新闻", f"给出昨天 Agent{tag} 框架的新闻", f"调研最近1天关于 Robot{tag} 硬件的新闻")
    r1 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research", "arguments": {"query": q1, "depth": "quick"}}, name="research___submit_research", timeout=120))
    r2 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research", "arguments": {"query": q2, "depth": "quick", "callback_url": "https://example.com/hook-b"}}, name="research___submit_research", timeout=120))
    r3 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research", "arguments": {"query": q3, "depth": "quick"}}, name="research___submit_research", timeout=120))
    assert r1["merged"] is False and r2["merged"] is True, (r1, r2)
    assert r1["task_id"] == r2["task_id"], (r1, r2)
    assert r3["task_id"] != r1["task_id"] and r3["merged"] is False, r3
    api = DeepResearchAPI(S)
    subs = api.store.list_subscriptions(r1["task_id"])
    assert len(subs) == 2 and any(s["callback_url"] == "https://example.com/hook-b" for s in subs)
    # cancel semantics: detaching one subscriber must NOT stop the shared backend
    c1 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___cancel_research", "arguments": {"task_id": r1["task_id"], "subscription_id": r2["subscription_id"]}}, name="research___cancel_research"))
    assert c1.get("remaining_subscribers") == 1 and c1.get("status") != "cancelled", c1
    assert (api.get(r1["task_id"]) or {}).get("status") != "cancelled"
    # without subscription_id on a still-shared task -> refused unless force
    api.store.add_subscription(r1["task_id"], actor_id="x", source="test")
    c2 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___cancel_research", "arguments": {"task_id": r1["task_id"]}}, name="research___cancel_research"))
    assert c2.get("status") != "cancelled" and c2.get("remaining_subscribers", 0) >= 2, c2
    # force stops it for everyone (cleanup); the unrelated task is cancelled too
    c3 = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___cancel_research", "arguments": {"task_id": r1["task_id"], "force": True}}, name="research___cancel_research"))
    assert c3["status"] == "cancelled", c3
    _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___cancel_research", "arguments": {"task_id": r3["task_id"], "force": True}}, name="research___cancel_research"))


@pytest.mark.timeout(300)
def test_live_research_default_is_async_and_merges(gateway_url):
    """Default `live___research` returns immediately (no stream) and merges an equivalent request."""
    tag = int(time.time()) % 100000
    a = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___research", "arguments": {"query": f"过去 7 天 Quantum{tag} 领域的重要进展", "depth": "quick"}}, name="live___research", timeout=120))
    b = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___research", "arguments": {"query": f"最近一周 Quantum{tag} 有什么大新闻", "depth": "quick"}}, name="live___research", timeout=120))
    assert a["status"] in ("queued", "running") and "task_id" in a and a["merged"] is False, a
    assert b["task_id"] == a["task_id"] and b["merged"] is True, (a, b)
    _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___cancel_research", "arguments": {"task_id": a["task_id"], "force": True}}, name="live___cancel_research"))


def test_dedupe_cache_ops_direct_or_lambda():
    from deepresearch.dedupe import DedupeClient
    c = DedupeClient(S)
    st = c.call("stats")
    assert "keys" in st and st["ttl_seconds"] == int(S["dedupe"]["ttl_seconds"]), st
    lk = c.call("lookup", query="调研最近1天的科技新闻")
    assert lk["canonical"]["time_from"] is not None, lk


# ---------------------------------------------------------------- standalone WebSearch MCP (stdio) + CLI
def test_websearch_cli_and_stdio_mcp():
    import subprocess
    script = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "websearch_mcp.py")
    r = subprocess.run([sys.executable, script, "--search", "Amazon Bedrock AgentCore harness", "--max", "3", "--json"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    data = json.loads(r.stdout)
    assert data["count"] >= 1 and data["results"][0]["url"].startswith("http")
    # stdio MCP server: list tools and call web_search through the MCP Python client
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=[script], env={**os.environ, "AWS_PROFILE": os.environ.get("AWS_PROFILE", MCP_PROFILE)})
        async with stdio_client(params) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                assert "web_search" in names, names
                res = await s.call_tool("web_search", {"query": "AgentCore Gateway rate limiting", "max_results": 2, "include_domains": ["aws.amazon.com"]})
                out = json.loads(res.content[0].text)
                assert out["count"] >= 1 and all("aws.amazon.com" in x["url"] for x in out["results"]), out
    asyncio.run(run())


@pytest.mark.timeout(300)
def test_cache_false_forces_fresh_task(gateway_url):
    """cache=false must neither merge into an equivalent task nor register itself for later merges."""
    tag = int(time.time()) % 100000
    q = f"最近一周 Sensor{tag} 芯片的重要进展"
    a = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___research", "arguments": {"query": q, "depth": "quick"}}, name="live___research", timeout=120))
    b = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___research", "arguments": {"query": q, "depth": "quick", "cache": False}}, name="live___research", timeout=120))
    c = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research", "arguments": {"query": q, "depth": "quick", "cache": False}}, name="research___submit_research", timeout=120))
    d = _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "research___submit_research", "arguments": {"query": q, "depth": "quick"}}, name="research___submit_research", timeout=120))
    assert a["merged"] is False and b["merged"] is False and b["task_id"] != a["task_id"], (a, b)
    assert c["merged"] is False and c["task_id"] not in (a["task_id"], b["task_id"]), c
    assert d["merged"] is True and d["task_id"] == a["task_id"], (a, d)   # cached path still merges with the cached task a
    for tid in {a["task_id"], b["task_id"], c["task_id"]}:
        _tool_json(_mcp_iam(gateway_url, "tools/call", {"name": "live___cancel_research", "arguments": {"task_id": tid, "force": True}}, name="live___cancel_research"))
