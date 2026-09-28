#!/usr/bin/env python
"""Deploy the deep-research service on AgentCore with boto3 (idempotent, dev tooling; production: deploy/cloudformation/).
Optionally reuses resources of an existing CloudFormation stack (settings.stack.name); region/account come from the
environment (DR_REGION / AWS_REGION / profile) and config/local.yaml.

Steps: stack outputs -> IAM roles -> Gateway (+Web Search target, rate limit) -> S3 bucket + skill upload
       -> SQS queues -> DynamoDB check -> harness (+endpoint) -> deploy_state.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botocore.exceptions import ClientError  # noqa: E402

from deepresearch.aws_clients import client  # noqa: E402

from deepresearch.config import load_settings, refresh_stack_outputs  # noqa: E402
from deepresearch.infra import gateway as gw  # noqa: E402
from deepresearch.infra import harness as hz  # noqa: E402
from deepresearch.infra import dedupe_infra, iam, live_mcp, mcp_tools, skills, storage  # noqa: E402

log = logging.getLogger("deploy")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness-region", help="override harness region (e.g. us-east-1 if cross-region gateway attach fails)")
    ap.add_argument("--network-mode", choices=["VPC", "PUBLIC"])
    ap.add_argument("--memory-mode", choices=["byo", "managed", "disabled"])
    ap.add_argument("--skip-harness", action="store_true")
    ap.add_argument("--only", choices=["gateway", "skills", "harness", "queues", "mcp-tools", "live", "dedupe"], help="run a single step")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s = load_settings()
    if args.harness_region:
        s.raw["harness"]["region"] = args.harness_region
    if args.network_mode:
        s.raw["harness"]["network_mode"] = args.network_mode
    if args.memory_mode:
        s.raw["harness"]["memory_mode"] = args.memory_mode
    if s.harness_region != s.region and s["harness"]["network_mode"] == "VPC":
        log.warning("harness region %s != stack region; VPC mode impossible -> PUBLIC", s.harness_region)
        s.raw["harness"]["network_mode"] = "PUBLIC"
    if s.harness_region != s.region and s["harness"]["memory_mode"] == "byo":
        log.warning("BYO memory is in the stack region; switching to managed memory")
        s.raw["harness"]["memory_mode"] = "managed"

    log.info("reading nx stack outputs")
    so = refresh_stack_outputs(s)
    log.info("VPC=%s subnets=%s SG=%s memory=%s table=%s", so.get("VpcId"),
             [so.get(k) for k in ("PrivateSubnetA", "PrivateSubnetB", "PrivateSubnetC")], so.get("DataServicesSG"),
             so.get("BedrockMemoryArn"), so.get("DynamoDBTableName"))

    if args.only in (None, "gateway"):
        gw_role = iam.ensure_gateway_role(s)
        g = gw.ensure_gateway(s, gw_role)
        t = gw.ensure_web_search_target(s, g["gatewayId"])
        rl = gw.ensure_rate_limit(s, g["gatewayId"])
        s.set_state(gateway_id=g["gatewayId"], gateway_arn=g["gatewayArn"], gateway_url=g["gatewayUrl"],
                    gateway_role_arn=gw_role, web_search_target_id=t["targetId"], gateway_rate_limit=bool(rl))
        log.info("gateway %s READY url=%s target=%s", g["gatewayId"], g["gatewayUrl"], t["targetId"])
        if args.only:
            return 0

    if args.only in (None, "skills"):
        bucket = storage.ensure_bucket(s)
        uri = skills.upload_skill(s)
        s.set_state(bucket=bucket, skills_uri=uri)
        log.info("skill uploaded to %s", uri)
        if args.only:
            return 0

    if args.only in (None, "queues"):
        q = storage.ensure_queues(s)
        tbl = storage.verify_table(s)
        n = storage.ensure_notifications(s)
        s.set_state(**q, table=tbl["table"], **n)
        log.info("notifications: SNS %s, webhook secret %s", n["sns_topic_arn"], n["webhook_secret_id"])
        log.info("queues %s ; table %s (ttl=%s)", q["queue_url"], tbl["table"], tbl["ttl"])
        if args.only:
            return 0

    if args.only in (None, "dedupe"):
        arn = dedupe_infra.ensure_dedupe_lambda(s, s.state("queue_url"))
        # the tools Lambda (gateway region, possibly cross-region) must be allowed to invoke it
        client("iam", s.region).put_role_policy(RoleName=f"{s['project']}-McpToolsLambdaRole", PolicyName="invoke-dedupe",
                                                    PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                                                        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}]}))
        s.set_state(dedupe_lambda_arn=arn)
        log.info("dedupe VPC lambda READY: %s", arn)
        if args.only:
            return 0

    if args.only in (None, "mcp-tools"):
        fn_arn = mcp_tools.ensure_lambda(s, s.state("queue_url"))
        t = mcp_tools.ensure_gateway_lambda_target(s, s.state("gateway_id"), fn_arn, s["gateway"]["role_name"])
        s.set_state(mcp_tools_lambda_arn=fn_arn, research_target_id=t["targetId"])
        log.info("MCP research tools target %s READY (lambda %s)", t["targetId"], fn_arn)
        if args.only:
            return 0

    if args.only in (None, "live"):
        image = live_mcp.build_and_push(s)
        rt = live_mcp.ensure_runtime(s, image)
        live_mcp.ensure_gateway_streaming(s, s.state("gateway_id"))
        t = live_mcp.ensure_live_target(s, s.state("gateway_id"), rt["agentRuntimeArn"], s["gateway"]["role_name"])
        s.set_state(live_runtime_arn=rt["agentRuntimeArn"], live_runtime_id=rt["agentRuntimeId"], live_image=image, live_target_id=t["targetId"])
        log.info("live streaming MCP target %s READY (runtime %s)", t["targetId"], rt["agentRuntimeArn"])
        if args.only:
            return 0

    if args.skip_harness:
        return 0
    role = iam.ensure_harness_role(s, s.state("gateway_arn"))
    s.set_state(harness_role_arn=role, harness_region=s.harness_region, network_mode=s["harness"]["network_mode"],
                memory_mode=s["harness"]["memory_mode"])
    time.sleep(5)
    try:
        h = hz.ensure_harness(s, role, s.state("gateway_arn"), s.state("skills_uri"))
    except ClientError as e:
        msg = str(e)
        log.error("CreateHarness/UpdateHarness failed: %s", msg)
        if "gateway" in msg.lower() and s.harness_region != s.gateway_region:
            log.error("Cross-region gateway attach rejected. Re-run: python scripts/deploy.py --harness-region %s", s.gateway_region)
        return 2
    s.set_state(harness_id=h["harnessId"], harness_arn=h["arn"], harness_version=h.get("harnessVersion"),
                harness_runtime_id=h.get("environment", {}).get("agentCoreRuntimeEnvironment", {}).get("agentRuntimeId"))
    log.info("harness READY: %s", h["arn"])
    print(json.dumps({k: s.state(k) for k in ("gateway_url", "harness_arn", "harness_region", "queue_url", "bucket",
                                                "network_mode", "memory_mode")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
