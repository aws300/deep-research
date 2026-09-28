#!/usr/bin/env python
"""Compute the quota values needed for a target concurrency and (optionally) file Service Quotas increase requests.

  python scripts/request_quotas.py --target 10000 --shards 1            # print plan
  python scripts/request_quotas.py --target 10000 --shards 1 --apply    # submit requests for quotas that exist in Service Quotas
Quotas that are not exposed in Service Quotas (Runtime active sessions) are printed as a support-case template.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.aws_clients import client  # noqa: E402
from deepresearch.capacity import TaskProfile, quotas_for_target  # noqa: E402
from deepresearch.config import load_settings  # noqa: E402

# (service, quota code, region-kind, description)
QUOTA_CODES = {
    "session_create_tps": ("bedrock-agentcore", "L-8EE2AEA2", "harness", "Rate of new Runtime session creation"),
    "runtime_dataplane_tps": ("bedrock-agentcore", "L-46ED137C", "harness", "Rate of Runtime data plane APIs"),
    "gateway_toolcall_tps_account": ("bedrock-agentcore", "L-A0D48779", "gateway", "Rate of tool-call/tool-list requests"),
    "gateway_toolcall_tps_gateway": ("bedrock-agentcore", "L-8CAB3FF3", "gateway", "Rate of tool-call/tool-list requests per gateway"),
    "gateway_connections": ("bedrock-agentcore", "L-6234C8FD", "gateway", "Tool-call/tool-list concurrent connections"),
    "websearch_tps": ("bedrock-agentcore", "L-84A99A88", "gateway", "Rate of Web Search Tool queries"),
    "memory_create_event_tps": ("bedrock-agentcore", "L-59AF2B24", "harness", "Rate of CreateEvent requests"),
    "memory_ltm_tpm": ("bedrock-agentcore", "L-E3D6644C", "harness", "Tokens per minute for long-term memory extraction"),
    "model_tpm_sonnet46": ("bedrock", "L-7BEE40FB", "harness", "Global cross-region TPM Claude Sonnet 4.6"),
    "model_tpm_haiku45": ("bedrock", "L-9A11C666", "harness", "Global cross-region TPM Claude Haiku 4.5"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=10000, help="concurrent executing tasks")
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--searches", type=int, default=30)
    ap.add_argument("--calls", type=int, default=40)
    ap.add_argument("--burndown", type=float, default=5.0)
    ap.add_argument("--cache", type=float, default=0.3)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    p = TaskProfile(minutes=a.minutes, searches=a.searches, model_calls=a.calls, output_burndown=a.burndown, cache_hit_ratio=a.cache)
    need = quotas_for_target(a.target, p, shards=a.shards)
    plan = {
        "session_create_tps": max(100, need["session_create_tps_for_5min_ramp"]),
        "runtime_dataplane_tps": max(2000, need["per_shard_C"] * 0.2),
        "gateway_toolcall_tps_account": max(600, need["gateway_toolcall_tps"] * 2),
        "gateway_toolcall_tps_gateway": max(600, need["gateway_toolcall_tps"] * 2),
        "gateway_connections": max(12000, need["gateway_connections"] * 1.2),
        "websearch_tps": max(50, need["websearch_tps"] * 1.2),
        "memory_create_event_tps": max(600, need["per_shard_C"] * 2 / 60 * 1.5),
        "memory_ltm_tpm": 1_000_000,
        "model_tpm_sonnet46": max(6_000_000, need["model_tpm"] * 0.6),
        "model_tpm_haiku45": max(5_000_000, need["model_tpm"] * 0.6),
    }
    print(json.dumps({"profile": p.__dict__, "needed_per_shard": need, "requests": plan}, indent=2))
    print("\n# Not in Service Quotas -> open an AWS Support case (Service limit increase):")
    print(f"#   Amazon Bedrock AgentCore Runtime 'Active session workloads per account' -> {need['runtime_active_sessions']} in {s.harness_region}")
    if not a.apply:
        return 0
    for key, value in plan.items():
        svc, code, kind, desc = QUOTA_CODES[key]
        region = s.harness_region if kind == "harness" else s.gateway_region
        sq = client("service-quotas", region)
        cur = sq.get_service_quota(ServiceCode=svc, QuotaCode=code)["Quota"]
        if float(cur["Value"]) >= float(value):
            print(f"skip {desc} ({region}): current {cur['Value']} >= {value}")
            continue
        r = sq.request_service_quota_increase(ServiceCode=svc, QuotaCode=code, DesiredValue=float(value))
        print(f"requested {desc} ({region}): {cur['Value']} -> {value} status={r['RequestedQuota']['Status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
