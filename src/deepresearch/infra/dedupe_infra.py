"""Deploy the VPC-attached dedupe Lambda (primary region, private subnets + data SG) that fronts Valkey."""
from __future__ import annotations

import io
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
from .mcp_tools import LAMBDA_TRUST


def policy(s: Settings, queue_arn: str) -> dict:
    tbl = f"arn:aws:dynamodb:{s.table_region}:{s.account_id}:table/{s.table_name}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"], "Resource": tbl},
        {"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"], "Resource": queue_arn},
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
         "Resource": f"arn:aws:secretsmanager:{s.region}:{s.account_id}:secret:{s['dedupe']['valkey_secret_id']}*"},
        {"Effect": "Allow", "Action": ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface",
                                       "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs", "ec2:AssignPrivateIpAddresses",
                                       "ec2:UnassignPrivateIpAddresses"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
    ]}


def build_zip() -> bytes:
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", td, "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:", "pyyaml", "redis>=5"], check=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in Path(td).rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(td).as_posix())
            for p in (ROOT / "src").rglob("*.py"):
                z.write(p, p.relative_to(ROOT).as_posix())
            z.write(ROOT / "config" / "settings.yaml", "config/settings.yaml")
            z.writestr("lambda_function.py", "from deepresearch.dedupe_lambda import lambda_handler\n")
        return buf.getvalue()


def ensure_dedupe_lambda(s: Settings, queue_url: str) -> str:
    region = s.region
    lam = client("lambda", region)
    queue_arn = client("sqs", s.queue_region).get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    role = ensure_role(f"{s['project']}-DedupeLambdaRole", LAMBDA_TRUST, {"dedupe": policy(s, queue_arn)}, "deep-research semantic dedupe (VPC, Valkey)")
    so = s.stack_outputs
    vpc = {"SubnetIds": [so[k] for k in ("PrivateSubnetA", "PrivateSubnetB", "PrivateSubnetC") if so.get(k)], "SecurityGroupIds": [so["DataServicesSG"]]}
    name = s["dedupe"]["lambda_name"]
    env = {"Variables": {"PYTHONPATH": "/var/task/src", "DR_DEDUPE_MODE": "direct", "DR_LOG_LEVEL": "INFO"}}
    code = build_zip()
    for attempt in range(8):
        try:
            try:
                lam.get_function(FunctionName=name)
                lam.update_function_configuration(FunctionName=name, Role=role, Timeout=60, MemorySize=1024, Environment=env, VpcConfig=vpc,
                                                  Handler="lambda_function.lambda_handler", Runtime="python3.12")
                _wait(lam, name)
                fn = lam.update_function_code(FunctionName=name, ZipFile=code, Architectures=["arm64"])
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                fn = lam.create_function(FunctionName=name, Runtime="python3.12", Role=role, Handler="lambda_function.lambda_handler",
                                         Code={"ZipFile": code}, Timeout=60, MemorySize=1024, Architectures=["arm64"], Environment=env,
                                         VpcConfig=vpc, Tags={"Project": s.project})
            break
        except ClientError as e:
            if ("role" in str(e).lower() or "assume" in str(e).lower()) and attempt < 7:
                time.sleep(10); continue
            raise
    _wait(lam, name)
    return fn["FunctionArn"]


def _wait(lam, name: str) -> None:
    for _ in range(90):
        c = lam.get_function_configuration(FunctionName=name)
        if c.get("LastUpdateStatus", "Successful") == "Successful" and c.get("State", "Active") == "Active":
            return
        time.sleep(3)
