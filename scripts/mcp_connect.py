#!/usr/bin/env python
"""Print how to connect an MCP client (Claude Code, Cursor, Claude Desktop...) to the deep-research Gateway.

Auth = a least-privilege IAM access key (only bedrock-agentcore:InvokeGateway on this gateway), signed with SigV4 by the
official stdio proxy `mcp-proxy-for-aws-cli`. No Cognito, no JWT.

  python scripts/mcp_connect.py                 # print claude mcp add command + .mcp.json snippet (uses the mcp_client.aws_profile profile)
  python scripts/mcp_connect.py --setup-profile # fetch the key from Secrets Manager and write ~/.aws/credentials [<mcp_client.aws_profile>]
  python scripts/mcp_connect.py --rotate        # create a new access key for the IAM user, store it, deactivate the old one
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.aws_clients import client  # noqa: E402
from deepresearch.config import load_settings  # noqa: E402

_S = load_settings()
SECRET_ID = _S.state("mcp_client_secret_id") or _S.raw["mcp_client"]["secret_id"]
PROFILE = _S.raw["mcp_client"]["aws_profile"]


def write_profile(key_id: str, secret: str, region: str) -> Path:
    p = Path.home() / ".aws" / "credentials"
    p.parent.mkdir(exist_ok=True)
    cp = configparser.RawConfigParser()
    cp.read(p)
    if not cp.has_section(PROFILE):
        cp.add_section(PROFILE)
    cp.set(PROFILE, "aws_access_key_id", key_id)
    cp.set(PROFILE, "aws_secret_access_key", secret)
    cp.set(PROFILE, "region", region)
    with open(p, "w") as f:
        cp.write(f)
    os.chmod(p, 0o600)
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-profile", action="store_true")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--name", default="nx-deep-research")
    a = ap.parse_args()
    s = load_settings()
    url, region = s.state("gateway_url"), s.gateway_region
    if not url:
        print("gateway not deployed (python scripts/deploy.py --only gateway)", file=sys.stderr)
        return 1
    sm = client("secretsmanager", s.region)
    if a.rotate:
        iam = client("iam", s.region)
        user = s["gateway"]["mcp_client_iam_user"]
        for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
            iam.update_access_key(UserName=user, AccessKeyId=k["AccessKeyId"], Status="Inactive")
        new = iam.create_access_key(UserName=user)["AccessKey"]
        sm.put_secret_value(SecretId=SECRET_ID, SecretString=json.dumps(
            {"aws_access_key_id": new["AccessKeyId"], "aws_secret_access_key": new["SecretAccessKey"], "region": region}))
        print("rotated: new key", new["AccessKeyId"], "(old keys set Inactive; delete them after clients are updated)")
        a.setup_profile = True
    if a.setup_profile:
        sec = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
        p = write_profile(sec["aws_access_key_id"], sec["aws_secret_access_key"], region)
        print(f"wrote profile [{PROFILE}] to {p}")
    cmd = (f"claude mcp add -s user -e AWS_PROFILE={PROFILE} -e AWS_REGION={region} --transport stdio {a.name} -- "
           f"uvx mcp-proxy-for-aws-cli@latest {url} --region {region} --service bedrock-agentcore --profile {PROFILE} --timeout 900 --read-timeout 900 --write-timeout 900 --tool-timeout 900")
    snippet = {"mcpServers": {a.name: {"command": "uvx", "args": ["mcp-proxy-for-aws-cli@latest", url, "--region", region,
                                                                 "--service", "bedrock-agentcore", "--profile", PROFILE,
                                                                 "--timeout", "900", "--read-timeout", "900", "--write-timeout", "900", "--tool-timeout", "900"],
                                         "timeout": 900000,
                                         "env": {"AWS_PROFILE": PROFILE, "AWS_REGION": region}}}}
    print("\n# Claude Code (CLI):\n" + cmd)
    print("\n# .mcp.json / claude_desktop_config.json / Cursor mcp.json:\n" + json.dumps(snippet, indent=2))
    print("\n# Direct HTTP (any MCP 2026-07-28 or 2025-11-25 client that can SigV4-sign):", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
