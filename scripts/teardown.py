#!/usr/bin/env python
"""Delete resources created by deploy.py (never touches the nx stack). Use --keep-gateway to keep the gateway."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.aws_clients import client  # noqa: E402
from deepresearch.config import DEPLOY_STATE, load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-gateway", action="store_true")
    ap.add_argument("--keep-bucket", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    if not a.yes:
        print("dry run; add --yes to delete:", {k: s.state(k) for k in ("harness_id", "gateway_id", "queue_url", "bucket")})
        return 0
    if s.state("harness_id"):
        client("bedrock-agentcore-control", s.harness_region).delete_harness(harnessId=s.state("harness_id"))
        print("deleted harness", s.state("harness_id"))
    if not a.keep_gateway:
        ctl = client("bedrock-agentcore-control", s.gateway_region)
        ids = [g for g in (None if s["gateway"]["reuse_gateway_id"] else s.state("gateway_id"),) if g]
        for gid in ids:
            for t in ctl.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
                ctl.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
            import time; time.sleep(10)
            ctl.delete_gateway(gatewayIdentifier=gid)
            print("deleted gateway", gid)
    # live streaming MCP runtime + its image repo + role
    try:
        rctl = client("bedrock-agentcore-control", s.harness_region)
        for r in rctl.list_agent_runtimes().get("agentRuntimes", []):
            if r["agentRuntimeName"] == s["live"]["runtime_name"]:
                rctl.delete_agent_runtime(agentRuntimeId=r["agentRuntimeId"]); print("deleted live runtime", r["agentRuntimeId"])
        client("ecr", s.harness_region).delete_repository(repositoryName=f"{s['project']}-live-mcp", force=True); print("deleted ecr repo")
    except Exception as e:  # noqa: BLE001
        print("live runtime cleanup:", e)
    try:
        client("lambda", s.region).delete_function(FunctionName=s["dedupe"]["lambda_name"]); print("deleted dedupe lambda")
        client("secretsmanager", s.region).delete_secret(SecretId=s["dedupe"]["valkey_secret_id"], ForceDeleteWithoutRecovery=True)
    except Exception as e:  # noqa: BLE001
        print("dedupe cleanup:", e)
    try:
        lam = client("lambda", s.gateway_region); lam.delete_function(FunctionName=f"{s['project']}-mcp-tools"); print("deleted mcp tools lambda")
    except Exception as e:  # noqa: BLE001
        print("lambda cleanup:", e)
    try:
        client("sns", s.queue_region).delete_topic(TopicArn=s.state("sns_topic_arn")); print("deleted sns topic")
    except Exception as e:  # noqa: BLE001
        print("sns cleanup:", e)
    sqs = client("sqs", s.queue_region)
    for k in ("queue_url", "dlq_url"):
        if s.state(k):
            sqs.delete_queue(QueueUrl=s.state(k)); print("deleted", s.state(k))
    if s.state("bucket") and not a.keep_bucket:
        s3 = client("s3", s.harness_region)
        pag = s3.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=s.state("bucket")):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=s.state("bucket"), Delete={"Objects": objs})
        s3.delete_bucket(Bucket=s.state("bucket")); print("deleted bucket", s.state("bucket"))
    iam = client("iam", s.region)
    for role in (s["harness"]["role_name"], s["gateway"]["role_name"], f"{s['project']}-LiveMcpRuntimeRole", f"{s['project']}-McpToolsLambdaRole", f"{s['project']}-DedupeLambdaRole"):
        try:
            for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            iam.delete_role(RoleName=role); print("deleted role", role)
        except Exception as e:  # noqa: BLE001
            print("role", role, e)
    try:  # least-privilege MCP client user + its stored key
        user = s["gateway"]["mcp_client_iam_user"]
        for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])
        for p in iam.list_user_policies(UserName=user)["PolicyNames"]:
            iam.delete_user_policy(UserName=user, PolicyName=p)
        iam.delete_user(UserName=user); print("deleted iam user", user)
        client("secretsmanager", s.region).delete_secret(SecretId=s["mcp_client"]["secret_id"], ForceDeleteWithoutRecovery=True)
    except Exception as e:  # noqa: BLE001
        print("mcp client user cleanup:", e)
    DEPLOY_STATE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
