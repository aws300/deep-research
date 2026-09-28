"""Settings loader — region/account agnostic.

Resolution order (later wins):
  1. config/settings.yaml                 generic defaults, names templated with {project} / {region} / {account_id}
  2. config/local.yaml (optional)         overlay for a specific environment (DR_LOCAL_FILE picks another file;
                                          skipped when DR_NO_LOCAL=1 or absent)
  3. environment                          DR_PROJECT, DR_REGION, DR_ACCOUNT_ID, DR_<SECTION>_<KEY>
Runtime state (ARNs, URLs of deployed resources) comes from env DR_STATE_<KEY> first (set by CloudFormation for Lambdas,
the live runtime and the ECS dispatcher), then from config/deploy_state.json (local tooling, scripts/use_stack.py).
Empty `region` fields inherit the top-level region = DR_REGION / AWS_REGION / AWS_DEFAULT_REGION / boto3 session region.
"""
from __future__ import annotations

import copy
import functools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("DR_CONFIG_DIR", ROOT / "config"))
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
LOCAL_FILE = Path(os.environ.get("DR_LOCAL_FILE", CONFIG_DIR / "local.yaml"))   # scripts/use_stack.py writes config/stacks/<stack>.yaml
STACK_CACHE = CONFIG_DIR / "stack_outputs.json"
DEPLOY_STATE = Path(os.environ.get("DR_DEPLOY_STATE", CONFIG_DIR / "deploy_state.json"))


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _coerce(raw: str, cur: Any) -> Any:
    if isinstance(cur, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(cur, int):
        return int(raw)
    if isinstance(cur, float):
        return float(raw)
    if isinstance(cur, list):
        return [x for x in raw.split(",") if x]
    return raw


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """DR_<SECTION>_<KEY>=value and DR_<TOPKEY>=value override the YAML (strings coerced to the YAML type)."""
    for key, values in data.items():
        if isinstance(values, dict):
            for sub in list(values.keys()):
                env_key = f"DR_{key}_{sub}".upper()
                if env_key in os.environ:
                    values[sub] = _coerce(os.environ[env_key], values[sub])
        else:
            env_key = f"DR_{key}".upper()
            if env_key in os.environ:
                data[key] = _coerce(os.environ[env_key], values)
    return data


def _default_region() -> str:
    for k in ("DR_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        import boto3
        return boto3.session.Session().region_name or ""
    except Exception:  # noqa: BLE001
        return ""


@functools.lru_cache(maxsize=1)
def _sts_account_id() -> str:
    import boto3
    return boto3.client("sts").get_caller_identity()["Account"]


def _render(data: Any, variables: dict[str, Any]) -> Any:
    """Recursively format {project}/{region}/{account_id} in string values (account id resolved lazily)."""
    if isinstance(data, dict):
        return {k: _render(v, variables) for k, v in data.items()}
    if isinstance(data, list):
        return [_render(v, variables) for v in data]
    if isinstance(data, str) and "{" in data:
        if "{account_id}" in data and not variables.get("account_id"):
            variables["account_id"] = _sts_account_id()
        try:
            return data.format(**variables)
        except (KeyError, IndexError, ValueError):
            return data
    return data


@dataclass
class Settings:
    raw: dict[str, Any]
    stack_outputs: dict[str, str] = field(default_factory=dict)
    deploy_state: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.raw[section]

    # --- identity / regions ------------------------------------------------------
    @property
    def project(self) -> str:
        return self.raw["project"]

    @property
    def region(self) -> str:
        return self.raw["region"]

    @property
    def account_id(self) -> str:
        if not self.raw.get("account_id"):
            self.raw["account_id"] = _sts_account_id()
        return str(self.raw["account_id"])

    @property
    def harness_region(self) -> str:
        return self.raw["harness"]["region"] or self.region

    @property
    def gateway_region(self) -> str:
        return self.raw["gateway"]["region"] or self.region

    @property
    def queue_region(self) -> str:
        return self.raw["queue"]["region"] or self.region

    @property
    def table_region(self) -> str:
        return self.raw["table"]["region"] or self.region

    @property
    def stack_region(self) -> str:
        return self.raw.get("stack", {}).get("region") or self.region

    # --- resources ------------------------------------------------------------------
    @property
    def bucket(self) -> str:
        return self.state("bucket") or self.raw["storage"]["bucket"]

    @property
    def workspace_dir(self) -> str:
        return self.raw["harness"]["workspace_dir"]

    @property
    def vpc_id(self) -> str:
        return self.stack_outputs.get("VpcId", "")

    @property
    def memory_arn(self) -> str:
        return self.state("memory_arn") or self.stack_outputs.get("BedrockMemoryArn", "")

    @property
    def table_name(self) -> str:
        return self.state("table") or self.raw["table"]["name"] or self.stack_outputs.get("DynamoDBTableName", "")

    @property
    def cognito_issuer(self) -> str:
        return self.stack_outputs.get("CognitoOidcIssuer", "")

    # --- runtime state ------------------------------------------------------------
    def state(self, key: str, default: Any = None) -> Any:
        env = os.environ.get(f"DR_STATE_{key.upper()}")
        if env not in (None, ""):
            return env
        return self.deploy_state.get(key, default)

    def set_state(self, **kwargs: Any) -> None:
        self.deploy_state.update(kwargs)
        DEPLOY_STATE.parent.mkdir(parents=True, exist_ok=True)
        DEPLOY_STATE.write_text(json.dumps(self.deploy_state, indent=2, default=str))

    @functools.cached_property
    def webhook_secret(self) -> str:
        """HMAC key for completion webhooks: explicit state value (legacy), else read from Secrets Manager by id."""
        legacy = self.deploy_state.get("webhook_secret") if not os.environ.get("DR_STATE_WEBHOOK_SECRET_ID") else None
        if legacy:
            return legacy
        sid = self.state("webhook_secret_id") or self.raw["notifications"]["webhook_secret_id"]
        from .aws_clients import client
        raw = client("secretsmanager", self.region).get_secret_value(SecretId=sid)["SecretString"]
        try:
            return json.loads(raw)["secret"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return raw


def load_settings(refresh_stack: bool = False) -> Settings:
    data = yaml.safe_load(SETTINGS_FILE.read_text())
    if LOCAL_FILE.exists() and os.environ.get("DR_NO_LOCAL") != "1":
        data = _deep_merge(data, yaml.safe_load(LOCAL_FILE.read_text()) or {})
    data = _apply_env_overrides(data)
    data["region"] = data.get("region") or _default_region()
    data = _render(data, {"project": data["project"], "region": data["region"], "account_id": data.get("account_id") or ""})
    stack_outputs: dict[str, str] = {}
    if STACK_CACHE.exists() and not refresh_stack and data.get("stack", {}).get("name"):
        stack_outputs = json.loads(STACK_CACHE.read_text())
    deploy_state = json.loads(DEPLOY_STATE.read_text()) if DEPLOY_STATE.exists() else {}
    return Settings(raw=data, stack_outputs=stack_outputs, deploy_state=deploy_state)


def refresh_stack_outputs(settings: Settings) -> dict[str, str]:
    """Optional: read outputs + selected physical ids of an existing CloudFormation stack (settings.stack.name) and cache them."""
    if not settings.raw.get("stack", {}).get("name"):
        settings.stack_outputs = {}
        return {}
    from .aws_clients import client

    cfn = client("cloudformation", settings.stack_region)
    name = settings["stack"]["name"]
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    wanted = {"PrivateSubnetA", "PrivateSubnetB", "PrivateSubnetC", "DataServicesSG", "S3Endpoint", "EfsFileSystem"}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=name):
        for r in page["StackResourceSummaries"]:
            if r["LogicalResourceId"] in wanted:
                outputs[r["LogicalResourceId"]] = r["PhysicalResourceId"]
    for k in list(outputs):  # never persist secrets from the stack
        if "Password" in k or "Secret" in k:
            outputs.pop(k)
    STACK_CACHE.write_text(json.dumps(outputs, indent=2))
    settings.stack_outputs = outputs
    return outputs
