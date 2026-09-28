"""Minimal MCP client for the Gateway (MCP 2026-07-28 stateless, SigV4). Used by e2e scripts and CLIs.

  call_tool(name, args)                    -> parsed JSON of the tool's first text content (raises McpError on tool error)
  stream_tool(name, args, on_event)        -> same, but consumes the SSE stream and forwards notifications to on_event(msg)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .config import Settings, load_settings

META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "nx-e2e", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {}}


class McpError(RuntimeError):
    pass


class GatewayClient:
    def __init__(self, settings: Settings | None = None, profile: str | None = None):
        self.s = settings or load_settings()
        profile = profile or os.environ.get("DR_MCP_PROFILE") or self.s.raw.get("mcp_client", {}).get("aws_profile", "")
        self.url = self.s.state("gateway_url")
        self.region = self.s.gateway_region
        try:
            self.creds = boto3.Session(profile_name=profile).get_credentials() if profile else None
        except Exception:  # noqa: BLE001  profile not configured -> default credentials
            self.creds = None
        self.creds = self.creds or boto3.Session().get_credentials()

    def _request(self, method: str, params: dict, name: str | None, accept: str, progress: bool = False):
        meta = dict(META)
        if progress:
            meta["progressToken"] = f"p{int(time.time() * 1000)}"
        body = json.dumps({"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method,
                           "params": {"_meta": meta, **params}}).encode()
        headers = {"Content-Type": "application/json", "Accept": accept, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = name
        req = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        SigV4Auth(self.creds, "bedrock-agentcore", self.region).add_auth(req)
        return urllib.request.Request(self.url, data=body, headers=dict(req.headers), method="POST")

    @staticmethod
    def _parse_body(raw: str) -> dict:
        if raw.lstrip().startswith(("event:", "data:")) or "\ndata:" in raw:
            raw = "".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
        return json.loads(raw)

    @staticmethod
    def _tool_payload(msg: dict):
        if "error" in msg:
            raise McpError(json.dumps(msg["error"], ensure_ascii=False))
        res = msg["result"]
        text = res["content"][0].get("text", "") if res.get("content") else ""
        if res.get("isError"):
            raise McpError(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    def list_tools(self) -> list[str]:
        with urllib.request.urlopen(self._request("tools/list", {}, None, "application/json, text/event-stream"), timeout=60) as r:
            return [t["name"] for t in self._parse_body(r.read().decode())["result"]["tools"]]

    def discover(self) -> dict:
        with urllib.request.urlopen(self._request("server/discover", {}, None, "application/json, text/event-stream"), timeout=60) as r:
            return self._parse_body(r.read().decode())["result"]

    def call_tool(self, name: str, args: dict, timeout: int = 120):
        try:
            # non-streaming calls ask for JSON only: with gateway streaming on, an SSE reply to a long call may keep the
            # connection open after the result and block read()
            with urllib.request.urlopen(self._request("tools/call", {"name": name, "arguments": args}, name,
                                                      "application/json"), timeout=timeout) as r:
                return self._tool_payload(self._parse_body(r.read().decode()))
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code}: {e.read().decode()[:300]}") from e

    def stream_tool(self, name: str, args: dict, on_event, timeout: int = 900, progress: bool = True):
        """SSE call. Always send a progressToken for long calls: without it the gateway buffers the response (no keep-alives)."""
        final = None
        with urllib.request.urlopen(self._request("tools/call", {"name": name, "arguments": args}, name,
                                                  "text/event-stream, application/json", progress=progress), timeout=timeout) as resp:
            if "text/event-stream" not in resp.headers.get("Content-Type", ""):
                return self._tool_payload(json.loads(resp.read().decode()))
            buf: list[str] = []
            for raw in resp:
                line = raw.decode().rstrip("\n").rstrip("\r")
                if line.startswith("data:"):
                    buf.append(line[5:].strip())
                elif line == "" and buf:
                    msg = json.loads("".join(buf))
                    buf = []
                    if "method" in msg:
                        on_event(msg)
                    elif "result" in msg or "error" in msg:
                        final = msg
                        break          # do not wait for the server to close the stream
        if final is None:
            raise McpError("stream ended without a result")
        return self._tool_payload(final)
