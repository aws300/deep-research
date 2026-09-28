"""Create / update the AgentCore harness that runs the deep-research skill."""
from __future__ import annotations

import time

from ..aws_clients import client
from ..config import Settings

SYSTEM_PROMPT = """You are Deep Research, a rigorous research analyst running inside Amazon Bedrock AgentCore.
Always load and follow the `deep-research-harness` skill for any research request.
Ground every factual statement in results returned by the WebSearch tool and cite them inline as [N].
Issue several independent WebSearch calls in the same turn whenever the sub-questions are independent
(they execute in parallel). Never invent URLs. Write all artifacts under {workspace}/ and finish with the
JSON status line described in the skill."""


def build_harness_config(s: Settings, role_arn: str, gateway_arn: str, skills_uri: str) -> dict:
    h = s["harness"]
    tools = [{"type": "agentcore_gateway", "name": "research_tools",
              "config": {"agentCoreGateway": {"gatewayArn": gateway_arn, "outboundAuth": {"awsIam": {}}}}}]
    cfg: dict = {
        "harnessName": h["name"],
        "executionRoleArn": role_arn,
        "model": {"bedrockModelConfig": {"modelId": h["model_id"], "maxTokens": int(h["max_tokens"]),
                                         "apiFormat": "converse_stream",
                                         **({"additionalParams": {"additionalModelRequestFields": {
                                             "thinking": {"type": "enabled", "budget_tokens": int(h["thinking_budget_tokens"])}}}}
                                            if int(h.get("thinking_budget_tokens", 0)) > 0 else {})}},
        "systemPrompt": [{"text": SYSTEM_PROMPT.format(workspace=s.workspace_dir)}],
        "tools": tools,
        "skills": [{"s3": {"uri": skills_uri}}],
        "maxIterations": int(h["max_iterations"]),
        "timeoutSeconds": int(h["timeout_seconds"]),
        "environment": {"agentCoreRuntimeEnvironment": {
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": int(h["idle_timeout_seconds"]),
                                       "maxLifetime": int(h["max_lifetime_seconds"])}}},
        "tags": {"Project": s.project},
    }
    if h["truncation"] == "summarization":
        cfg["truncation"] = {"strategy": "summarization",
                             "config": {"summarization": {"summaryRatio": 0.3, "preserveRecentMessages": 10}}}
    elif h["truncation"] == "sliding_window":
        cfg["truncation"] = {"strategy": "sliding_window", "config": {"slidingWindow": {"messagesCount": 40}}}
    # networking: reuse the nx stack VPC (private subnets w/ NAT + DataServicesSG) when harness lives in the stack region
    if h["network_mode"] == "VPC":
        so = s.stack_outputs
        subnets = [so[k] for k in ("PrivateSubnetA", "PrivateSubnetB", "PrivateSubnetC") if so.get(k)]
        if not subnets or not so.get("DataServicesSG"):
            raise RuntimeError("VPC mode requested but stack subnets/SG unknown; run refresh_stack_outputs first")
        cfg["environment"]["agentCoreRuntimeEnvironment"]["networkConfiguration"] = {
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": subnets, "securityGroups": [so["DataServicesSG"]]}}
    else:
        cfg["environment"]["agentCoreRuntimeEnvironment"]["networkConfiguration"] = {"networkMode": "PUBLIC"}
    # memory
    if h["memory_mode"] == "byo" and s.memory_arn:
        cfg["memory"] = {"agentCoreMemoryConfiguration": {"arn": s.memory_arn, "messagesCount": 20}}
    elif h["memory_mode"] == "managed":
        cfg["memory"] = {"managedMemoryConfiguration": {"strategies": ["SEMANTIC", "SUMMARIZATION"], "eventExpiryDuration": 30}}
    else:
        cfg["memory"] = {"disabled": {}}
    return cfg


def find_harness(s: Settings) -> dict | None:
    ctl = client("bedrock-agentcore-control", s.harness_region)
    for hs in ctl.list_harnesses().get("harnesses", []):
        if hs["harnessName"] == s["harness"]["name"]:
            return ctl.get_harness(harnessId=hs["harnessId"])["harness"]
    return None


def wait_ready(s: Settings, harness_id: str, timeout: int = 900) -> dict:
    ctl = client("bedrock-agentcore-control", s.harness_region)
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = ctl.get_harness(harnessId=harness_id)["harness"]
        if h["status"] == "READY":
            return h
        if h["status"].endswith("FAILED"):
            raise RuntimeError(f"harness {harness_id} {h['status']}: {h.get('failureReason')}")
        time.sleep(10)
    raise TimeoutError("harness not READY in time")


def ensure_harness(s: Settings, role_arn: str, gateway_arn: str, skills_uri: str) -> dict:
    ctl = client("bedrock-agentcore-control", s.harness_region)
    cfg = build_harness_config(s, role_arn, gateway_arn, skills_uri)
    existing = find_harness(s)
    if existing:
        upd = {k: v for k, v in cfg.items() if k not in ("harnessName", "tags")}
        # note: requireServiceS3Endpoint is immutable for agents created after 2026-06-11 -> never set it
        if "memory" in upd:  # UpdateHarness wraps nullable structures in optionalValue
            upd["memory"] = {"optionalValue": upd["memory"]}
        h = ctl.update_harness(harnessId=existing["harnessId"], **upd)["harness"]
    else:
        h = ctl.create_harness(**cfg)["harness"]
    return wait_ready(s, h["harnessId"])
