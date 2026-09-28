"""Build/push the streaming MCP server image, host it on AgentCore Runtime (MCP protocol), attach to the Gateway."""
from __future__ import annotations

import base64
import os
import json
import subprocess
import time
import urllib.parse

from botocore.exceptions import ClientError

from ..aws_clients import client
from ..config import ROOT, Settings
from .iam import AGENTCORE_TRUST, ensure_role



def runtime_policy(s: Settings) -> dict:
    tbl = f"arn:aws:dynamodb:{s.table_region}:{s.account_id}:table/{s.table_name}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"], "Resource": tbl},
        {"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"],
         "Resource": f"arn:aws:sqs:{s.queue_region}:{s.account_id}:{s['queue']['name']}"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{s.bucket}", f"arn:aws:s3:::{s.bucket}/*"]},
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": f"arn:aws:lambda:{s.region}:{s.account_id}:function:{s['dedupe']['lambda_name']}"},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": f"arn:aws:secretsmanager:{s.region}:{s.account_id}:secret:{s['dedupe']['valkey_secret_id']}*"},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:StopRuntimeSession"],
         "Resource": [f"arn:aws:bedrock-agentcore:{s.harness_region}:{s.account_id}:runtime/*",
                      f"arn:aws:bedrock-agentcore:{s.harness_region}:{s.account_id}:harness/*"]},
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups",
                                       "logs:DescribeLogStreams", "xray:PutTraceSegments", "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                       "bedrock-agentcore:GetWorkloadAccessTokenForUserId"], "Resource": "*"},
    ]}


def build_and_push(s: Settings) -> str:
    ecr = client("ecr", s.harness_region)
    repo = f"{s['project']}-live-mcp"
    try:
        uri = ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]["repositoryUri"]
    except ClientError:
        uri = ecr.create_repository(repositoryName=repo, imageScanningConfiguration={"scanOnPush": True},
                                    tags=[{"Key": "Project", "Value": s.project}])["repository"]["repositoryUri"]
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, pwd = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"]
    subprocess.run(["docker", "login", "--username", user, "--password-stdin", registry], input=pwd.encode(), check=True, capture_output=True)
    # public.ecr.aws base image pulls are rate-limited / 403 for anonymous clients -> authenticate
    pub = client("ecr-public", "us-east-1").get_authorization_token()["authorizationData"]["authorizationToken"]
    puser, ppwd = base64.b64decode(pub).decode().split(":", 1)
    subprocess.run(["docker", "login", "--username", puser, "--password-stdin", "public.ecr.aws"], input=ppwd.encode(), check=True, capture_output=True)
    tag = f"{uri}:{int(time.time())}"
    for cmd in (["docker", "build", "--platform", "linux/arm64", "-f", str(ROOT / "deploy" / "live-mcp.Dockerfile"), "-t", tag, str(ROOT)],
                ["docker", "push", tag]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])} failed:\n{r.stderr[-2000:]}")
    return tag


def ensure_runtime(s: Settings, image: str) -> dict:
    ctl = client("bedrock-agentcore-control", s.harness_region)
    role = ensure_role(f"{s['project']}-LiveMcpRuntimeRole", AGENTCORE_TRUST, {"live": runtime_policy(s)},
                       "deep-research streaming MCP server runtime role")
    time.sleep(5)
    existing = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", []) if r["agentRuntimeName"] == s["live"]["runtime_name"]), None)
    kwargs = {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}}, "roleArn": role,
              "networkConfiguration": {"networkMode": "PUBLIC"}, "protocolConfiguration": {"serverProtocol": "MCP"},
              "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 28800},
              "environmentVariables": {"DR_LIVE": "1"}}
    for attempt in range(8):  # IAM propagation: ECR permissions of a freshly created role take a while to be visible
        try:
            if existing:
                r = ctl.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **kwargs)
                rid = existing["agentRuntimeId"]
            else:
                r = ctl.create_agent_runtime(agentRuntimeName=s["live"]["runtime_name"], description="deep-research streaming MCP server (research/watch/cancel)",
                                             tags={"Project": s.project}, **kwargs)
                rid = r["agentRuntimeId"]
            break
        except ClientError as e:
            if "Access denied while validating ECR" in str(e) and attempt < 7:
                time.sleep(15)
                continue
            raise
    for _ in range(90):
        g = ctl.get_agent_runtime(agentRuntimeId=rid)
        if g["status"] == "READY":
            return g
        if g["status"].endswith("FAILED"):
            raise RuntimeError(f"runtime {rid} {g['status']}: {g.get('failureReason')}")
        time.sleep(10)
    raise TimeoutError("runtime not READY")


def ensure_gateway_streaming(s: Settings, gateway_id: str) -> None:
    ctl = client("bedrock-agentcore-control", s.gateway_region)
    g = ctl.get_gateway(gatewayIdentifier=gateway_id)
    mcp_cfg = g["protocolConfiguration"]["mcp"]
    if mcp_cfg.get("streamingConfiguration", {}).get("enableResponseStreaming"):
        return
    mcp_cfg["streamingConfiguration"] = {"enableResponseStreaming": True}
    ctl.update_gateway(gatewayIdentifier=gateway_id, name=g["name"], roleArn=g["roleArn"], protocolType="MCP",
                       authorizerType=g["authorizerType"], protocolConfiguration={"mcp": mcp_cfg})
    for _ in range(30):
        if ctl.get_gateway(gatewayIdentifier=gateway_id)["status"] == "READY":
            return
        time.sleep(5)


def ensure_live_target(s: Settings, gateway_id: str, runtime_arn: str, gateway_role_name: str) -> dict:
    iam = client("iam", s.region)
    iam.put_role_policy(RoleName=gateway_role_name, PolicyName="invoke-live-mcp-runtime", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                                 "Resource": [runtime_arn, runtime_arn + "/*"]}]}))
    endpoint = (f"https://bedrock-agentcore.{s.harness_region}.amazonaws.com/runtimes/"
                f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")
    ctl = client("bedrock-agentcore-control", s.gateway_region)
    cfg = {"mcp": {"mcpServer": {"endpoint": endpoint}}}
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE",
              "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore", "region": s.harness_region}}}]
    name = "live"
    existing = next((t for t in ctl.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []) if t["name"] == name), None)
    if existing:
        t = ctl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["targetId"], name=name,
                                      description="deep-research live: research (streaming) / watch / cancel",
                                      targetConfiguration=cfg, credentialProviderConfigurations=creds)
    else:
        t = ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=name,
                                      description="deep-research live: research (streaming) / watch / cancel",
                                      targetConfiguration=cfg, credentialProviderConfigurations=creds)
    for attempt in range(3):
        for _ in range(60):
            t = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
            if t["status"] == "READY":
                return t
            if t["status"].endswith("FAILED"):
                break
            time.sleep(5)
        reasons = " ".join(t.get("statusReasons") or [])
        if "Authorization" in reasons and attempt < 2:
            # freshly attached InvokeAgentRuntime permission on the gateway role has not propagated yet -> recreate
            ctl.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
            time.sleep(30)
            t = ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=name,
                                          description="deep-research live: research (streaming) / watch / cancel",
                                          targetConfiguration=cfg, credentialProviderConfigurations=creds)
            continue
        raise RuntimeError(f"live target failed: {t.get('statusReasons')}")
    return t
