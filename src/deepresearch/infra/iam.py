"""IAM roles for the Gateway (outbound Web Search) and the harness execution role. Idempotent."""
from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from ..aws_clients import client
from ..config import Settings

AGENTCORE_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                   "Action": ["sts:AssumeRole", "sts:TagSession"]}],
}


def ensure_role(name: str, trust: dict, inline_policies: dict[str, dict], description: str, project: str = "deepresearch") -> str:
    from ..config import _default_region
    iam = client("iam", _default_region() or "us-east-1")  # IAM is global; any valid region endpoint works
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description=description, Tags=[{"Key": "Project", "Value": project}])["Role"]["Arn"]
        time.sleep(8)  # IAM propagation
    for pname, doc in inline_policies.items():
        iam.put_role_policy(RoleName=name, PolicyName=pname, PolicyDocument=json.dumps(doc))
    return arn


def gateway_role_policy(s: Settings) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "InvokeGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
             "Resource": f"arn:aws:bedrock-agentcore:{s.gateway_region}:{s.account_id}:gateway/*"},
            {"Sid": "InvokeWebSearch", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
             "Resource": f"arn:aws:bedrock-agentcore:{s.gateway_region}:aws:tool/web-search.v1"},
        ],
    }


def harness_exec_policy(s: Settings, gateway_arn: str) -> dict:
    stmts = [
        {"Sid": "BedrockInference", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail",
                    "bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"],
         "Resource": "*"},
        {"Sid": "GatewayTools", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"], "Resource": gateway_arn},
        {"Sid": "Skills", "Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{s.bucket}", f"arn:aws:s3:::{s.bucket}/*"]},
        {"Sid": "Observability", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData"],
         "Resource": "*"},
        {"Sid": "WorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": "*"},
        {"Sid": "ECR", "Effect": "Allow",
         "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": "*"},
    ]
    if s["harness"]["memory_mode"] in ("byo", "managed"):
        mem_res = s.memory_arn if s["harness"]["memory_mode"] == "byo" and s.memory_arn else \
            f"arn:aws:bedrock-agentcore:{s.harness_region}:{s.account_id}:memory/*"
        stmts.append({"Sid": "Memory", "Effect": "Allow",
                      "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent", "bedrock-agentcore:ListEvents",
                                 "bedrock-agentcore:ListSessions", "bedrock-agentcore:RetrieveMemoryRecords",
                                 "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:GetMemoryRecord",
                                 "bedrock-agentcore:GetMemory", "bedrock-agentcore:DeleteEvent"],
                      "Resource": [mem_res, mem_res.rsplit("/", 1)[0] + "/*"] if mem_res else "*"})
    if s["harness"]["network_mode"] == "VPC":
        stmts.append({"Sid": "VpcNetworking", "Effect": "Allow",
                      "Action": ["ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
                                 "ec2:DescribeVpcs", "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
                                 "ec2:DescribeRouteTables", "ec2:DescribeVpcEndpoints"],
                      "Resource": "*"})
    return {"Version": "2012-10-17", "Statement": stmts}


def ensure_gateway_role(s: Settings) -> str:
    return ensure_role(s["gateway"]["role_name"], AGENTCORE_TRUST, {"websearch": gateway_role_policy(s)},
                       "deep-research Gateway service role (Web Search connector)")


def ensure_harness_role(s: Settings, gateway_arn: str) -> str:
    return ensure_role(s["harness"]["role_name"], AGENTCORE_TRUST, {"harness": harness_exec_policy(s, gateway_arn)},
                       "deep-research AgentCore harness execution role")
