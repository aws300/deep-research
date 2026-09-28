#!/usr/bin/env python
"""Standalone Web Search MCP server (stdio) + CLI. Uses only the Gateway's Amazon Web Search connector; no research pipeline.

MCP (Claude Code / Cursor / Claude Desktop):
  claude mcp add -s user -e AWS_PROFILE=<mcp-profile> -e NX_GATEWAY_URL=<gateway-url> websearch -- <repo>/.venv/bin/python \
      <repo>/scripts/websearch_mcp.py
  -> tool `web_search(query, max_results=8, include_domains=[], exclude_domains=[], from_date=None, to_date=None)`

CLI:
  python scripts/websearch_mcp.py --search "AgentCore Runtime V2" --max 5 [--json] [--include aws.amazon.com] [--from 2026-09-01]

Auth: SigV4 with DR_MCP_PROFILE / AWS_PROFILE / settings mcp_client.aws_profile (least-privilege InvokeGateway), else default credentials.\nGateway: NX_GATEWAY_URL / DR_STATE_GATEWAY_URL / config/deploy_state.json; region parsed from the URL.
Speaks MCP 2026-07-28 (stateless) to the Gateway; exposes MCP to the local client via the Python SDK (stdio).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ROOT = Path(__file__).resolve().parents[1]


def _gateway_url() -> str:
    url = os.environ.get("NX_GATEWAY_URL") or os.environ.get("DR_STATE_GATEWAY_URL")
    if not url:
        try:
            url = json.loads((ROOT / "config" / "deploy_state.json").read_text())["gateway_url"]
        except Exception:  # noqa: BLE001
            url = ""
    if not url:
        sys.exit("Gateway URL unknown: set NX_GATEWAY_URL or run scripts/use_stack.py <stack> to write config/deploy_state.json")
    return url


def _profile() -> str:
    prof = os.environ.get("DR_MCP_PROFILE") or os.environ.get("AWS_PROFILE")
    if prof:
        return prof
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from deepresearch.config import load_settings
        return load_settings().raw.get("mcp_client", {}).get("aws_profile", "")
    except Exception:  # noqa: BLE001
        return ""


GATEWAY_URL = _gateway_url()
_m = re.search(r"\.gateway\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com", GATEWAY_URL)
GATEWAY_REGION = os.environ.get("NX_GATEWAY_REGION") or (_m.group(1) if _m else os.environ.get("AWS_REGION", ""))
TOOL_NAME = os.environ.get("NX_WEBSEARCH_TOOL", "web-search___WebSearch")
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "nx-websearch-mcp", "version": "0.1"},
        "io.modelcontextprotocol/clientCapabilities": {}}


def _creds():
    prof = _profile()
    try:
        c = boto3.Session(profile_name=prof).get_credentials() if prof else None
        if c:
            return c
    except Exception:  # noqa: BLE001  profile not configured -> default credentials
        pass
    return boto3.Session().get_credentials()


def web_search(query: str, max_results: int = 8, include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
               from_date: str | None = None, to_date: str | None = None) -> dict:
    """Search the web via Amazon Web Search on AgentCore. Returns {results:[{title,url,text,publishedDate}], count}."""
    args: dict = {"query": query[:200], "maxResults": max(1, min(int(max_results), 25))}
    filters: dict = {}
    if include_domains or exclude_domains:
        filters["domainFilter"] = {k: v for k, v in (("include", include_domains), ("exclude", exclude_domains)) if v}
    if from_date or to_date:
        filters["publishedDateFilter"] = {k: v for k, v in (("from", from_date), ("to", to_date)) if v}
    if filters:
        args["filters"] = filters
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"_meta": META, "name": TOOL_NAME, "arguments": args}}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "MCP-Protocol-Version": "2026-07-28",
               "Mcp-Method": "tools/call", "Mcp-Name": TOOL_NAME}
    req = AWSRequest(method="POST", url=GATEWAY_URL, data=body, headers=headers)
    SigV4Auth(_creds(), "bedrock-agentcore", GATEWAY_REGION).add_auth(req)
    r = urllib.request.Request(GATEWAY_URL, data=body, headers=dict(req.headers), method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    if "error" in out:
        return {"error": out["error"]}
    res = out["result"]
    if res.get("isError"):
        return {"error": res["content"][0].get("text", "tool error")}
    data = json.loads(res["content"][0]["text"])
    results = [{"title": x.get("title"), "url": x.get("url"), "publishedDate": x.get("publishedDate"), "text": x.get("text")}
               for x in data.get("results", [])]
    return {"query": query, "count": len(results), "results": results}


def serve_stdio() -> None:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("nx-websearch", instructions="Amazon Web Search (AgentCore). Use web_search for current web information; cite the returned URLs.")
    mcp.tool()(web_search)
    mcp.run(transport="stdio")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", metavar="QUERY", help="CLI mode: run one search and print results")
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--from", dest="from_date")
    ap.add_argument("--to", dest="to_date")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.search:
        serve_stdio(); return 0
    out = web_search(a.search, a.max, a.include or None, a.exclude or None, a.from_date, a.to_date)
    if a.json or "error" in out:
        print(json.dumps(out, ensure_ascii=False, indent=2)); return 0 if "error" not in out else 1
    for i, r in enumerate(out["results"], 1):
        print(f"{i}. {r['title']}\n   {r['url']}  ({r['publishedDate']})\n   {(r['text'] or '')[:220].replace(chr(10), ' ')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
