"""Package + deploy the MCP research tools Lambda and attach it as a Gateway Lambda target."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from ..aws_clients import client
from ..config import ROOT, Settings
from .iam import ensure_role

LAMBDA_TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                        "Action": "sts:AssumeRole"}]}
TOOL_SCHEMA = [
    {"name": "submit_research",
     "description": "Start an asynchronous deep-research task and return a task_id. Semantically equivalent requests within 12h are "
                    "merged into the same task_id (merged=true). "
                    "quick ~3-6 min / standard ~10-20 min / deep ~20-40 min. Poll get_research_status, then get_research_report.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Research question or brief (any language)."},
         "depth": {"type": "string", "description": "Research depth: quick | standard | deep. Default standard."},
         "callback_url": {"type": "string", "description": "Optional https:// webhook. On completion the service POSTs a signed JSON "
                                                              "(task_id, status, report_s3, download_url) so nobody has to poll."},
         "cache": {"type": "boolean", "description": "Default true: merge with an equivalent task submitted in the last 12h. "
                                                     "false: always start a fresh, independent task."}},
         "required": ["query"]}},
    {"name": "run_research",
     "description": "Run a research and wait up to 300 s (sync cap) in this call; returns the Markdown report if finished, otherwise "
                    "status=running + task_id. For a single blocking call that always completes, prefer live___research_live (streams keep-alives).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Research question or brief (any language)."},
         "depth": {"type": "string", "description": "quick (recommended for synchronous use). Default quick."},
         "wait_seconds": {"type": "integer", "description": "Max seconds to wait (30-300). Default 300."},
         "cache": {"type": "boolean", "description": "Default true: reuse an equivalent recent task; false: fresh task."}},
         "required": ["query"]}},
    {"name": "cancel_research",
     "description": "Detach from / cancel a research task. With subscription_id only your callback/stream is detached; the shared "
                    "backend research is stopped only when no other subscriber remains (or force=true).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "subscription_id": {"type": "string"},
                                                     "force": {"type": "boolean", "description": "stop the backend even if shared"}},
                     "required": ["task_id"]}},
    {"name": "get_research_status",
     "description": "Status, progress counters and the most recent reasoning / tool steps of a research task.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "get_research_report",
     "description": "Fetch the finished Markdown report. mode=content returns the Markdown text (save it as a .md file); "
                    "mode=url returns a 1-hour presigned S3 download URL plus the s3:// path.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"},
                                                     "mode": {"type": "string", "description": "content | url. Default content."}},
                     "required": ["task_id"]}},
]


def lambda_policy(s: Settings, queue_arn: str) -> dict:
    tbl = f"arn:aws:dynamodb:{s.table_region}:{s.account_id}:table/{s.table_name}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"], "Resource": queue_arn},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                                       "dynamodb:BatchWriteItem"], "Resource": tbl},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{s.bucket}", f"arn:aws:s3:::{s.bucket}/*"]},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": f"arn:aws:secretsmanager:{s.region}:{s.account_id}:secret:{s['dedupe']['valkey_secret_id']}*"},
    ]}


def build_zip() -> bytes:
    """src/ + config/settings.yaml + vendored pyyaml (not in the Lambda base image)."""
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", td, "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:", "pyyaml"], check=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in Path(td).rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(td).as_posix())
            for p in (ROOT / "src").rglob("*.py"):
                z.write(p, p.relative_to(ROOT).as_posix())
            z.write(ROOT / "config" / "settings.yaml", "config/settings.yaml")
            z.writestr("lambda_function.py", "from deepresearch.mcp_tools_lambda import lambda_handler\n")
        return buf.getvalue()


def ensure_lambda(s: Settings, queue_url: str) -> str:
    lam = client("lambda", s.gateway_region)
    sqs = client("sqs", s.queue_region)
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    role = ensure_role(f"{s['project']}-McpToolsLambdaRole", LAMBDA_TRUST, {"tools": lambda_policy(s, queue_arn)},
                       "deep-research MCP tools Lambda")
    name = f"{s['project']}-mcp-tools"
    code = build_zip()
    env = {"Variables": {"QUEUE_URL": queue_url, "PYTHONPATH": "/var/task/src"}}
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, Role=role, Timeout=840, MemorySize=512, Environment=env,
                                          Handler="lambda_function.lambda_handler", Runtime="python3.12")
        _wait(lam, name)
        fn = lam.update_function_code(FunctionName=name, ZipFile=code, Architectures=["arm64"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        for _ in range(6):  # role propagation
            try:
                fn = lam.create_function(FunctionName=name, Runtime="python3.12", Role=role, Handler="lambda_function.lambda_handler",
                                         Code={"ZipFile": code}, Timeout=840, MemorySize=512, Architectures=["arm64"],
                                         Environment=env, Tags={"Project": s.project})
                break
            except ClientError as ce:
                if "role" in str(ce).lower():
                    time.sleep(10)
                    continue
                raise
    _wait(lam, name)
    return fn["FunctionArn"]


def _wait(lam, name: str) -> None:
    for _ in range(60):
        c = lam.get_function_configuration(FunctionName=name)
        if c.get("LastUpdateStatus", "Successful") == "Successful" and c.get("State", "Active") == "Active":
            return
        time.sleep(3)


def ensure_gateway_lambda_target(s: Settings, gateway_id: str, lambda_arn: str, gateway_role_name: str) -> dict:
    iam = client("iam", s.region)
    iam.put_role_policy(RoleName=gateway_role_name, PolicyName="invoke-mcp-tools-lambda", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}]}))
    ctl = client("bedrock-agentcore-control", s.gateway_region)
    name = "research"
    cfg = {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": TOOL_SCHEMA}}}}
    existing = next((t for t in ctl.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []) if t["name"] == name), None)
    if existing:
        t = ctl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["targetId"], name=name,
                                      description="deep-research pipeline: submit / status / report",
                                      targetConfiguration=cfg,
                                      credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
    else:
        t = ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=name,
                                      description="deep-research pipeline: submit / status / report",
                                      targetConfiguration=cfg,
                                      credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
    for _ in range(60):
        t = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        if t["status"] == "READY":
            return t
        if t["status"].endswith("FAILED"):
            raise RuntimeError(f"research target failed: {t.get('statusReasons')}")
        time.sleep(5)
    return t
