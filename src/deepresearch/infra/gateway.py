"""AgentCore Gateway with the built-in Web Search connector (the gateway region must offer the connector)."""
from __future__ import annotations

import time

from ..aws_clients import client
from ..config import Settings


def _ctl(s: Settings):
    return client("bedrock-agentcore-control", s.gateway_region)


def find_gateway(s: Settings) -> dict | None:
    ctl = _ctl(s)
    if s["gateway"]["reuse_gateway_id"]:
        return ctl.get_gateway(gatewayIdentifier=s["gateway"]["reuse_gateway_id"])
    for g in ctl.list_gateways().get("items", []):
        if g["name"] == s["gateway"]["name"]:
            return ctl.get_gateway(gatewayIdentifier=g["gatewayId"])
    return None


def _find_by_name(s: Settings, name: str) -> dict | None:
    ctl = _ctl(s)
    for g in ctl.list_gateways().get("items", []):
        if g["name"] == name:
            return ctl.get_gateway(gatewayIdentifier=g["gatewayId"])
    return None


def _create_or_get(s: Settings, name: str, role_arn: str, versions: list[str], description: str, instructions: str,
                   auth: str = "AWS_IAM") -> dict:
    ctl = _ctl(s)
    gw = _find_by_name(s, name)
    auth_kwargs = {"authorizerType": auth}
    if gw is None:
        gw = ctl.create_gateway(
            name=name, description=description, roleArn=role_arn, protocolType="MCP",
            protocolConfiguration={"mcp": {"supportedVersions": list(versions), "searchType": "SEMANTIC",
                                           "instructions": instructions}},
            tags={"Project": s.project}, **auth_kwargs,
        )
    elif gw.get("authorizerType") != auth:
        # authorizer type is immutable -> delete (targets first) and recreate with the requested auth
        for t in ctl.list_gateway_targets(gatewayIdentifier=gw["gatewayId"]).get("items", []):
            ctl.delete_gateway_target(gatewayIdentifier=gw["gatewayId"], targetId=t["targetId"])
        for _ in range(30):
            if not ctl.list_gateway_targets(gatewayIdentifier=gw["gatewayId"]).get("items"):
                break
            time.sleep(3)
        ctl.delete_gateway(gatewayIdentifier=gw["gatewayId"])
        for _ in range(60):
            if _find_by_name(s, name) is None:
                break
            time.sleep(3)
        return _create_or_get(s, name, role_arn, versions, description, instructions, auth)
    elif sorted(gw["protocolConfiguration"]["mcp"].get("supportedVersions", [])) != sorted(versions):
        gw = ctl.update_gateway(gatewayIdentifier=gw["gatewayId"], name=name, roleArn=role_arn, protocolType="MCP",
                                protocolConfiguration={"mcp": {**gw["protocolConfiguration"]["mcp"],
                                                               "supportedVersions": list(versions)}}, **auth_kwargs)
    return gw


def ensure_gateway(s: Settings, role_arn: str) -> dict:
    ctl = _ctl(s)
    gw = find_gateway(s) if s["gateway"]["reuse_gateway_id"] else None
    if gw is None:
        gw = _create_or_get(s, s["gateway"]["name"], role_arn, s["gateway"]["supported_versions"],
                            "deep-research tools gateway (Web Search connector)",
                            "Research tools for the deep-research harness.")
    gid = gw["gatewayId"]
    for _ in range(60):
        gw = ctl.get_gateway(gatewayIdentifier=gid)
        if gw["status"] == "READY":
            break
        if gw["status"].endswith("FAILED"):
            raise RuntimeError(f"gateway {gid} failed: {gw.get('statusReasons')}")
        time.sleep(5)
    return gw


def ensure_web_search_target(s: Settings, gateway_id: str) -> dict:
    ctl = _ctl(s)
    name = s["gateway"]["web_search_target_name"]
    for t in ctl.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []):
        if t["name"] == name:
            return t
    params = {}
    if s["gateway"]["domain_exclude"]:
        params["domainFilter"] = {"exclude": list(s["gateway"]["domain_exclude"])}
    t = ctl.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=name,
        description="Amazon-managed Web Search (agentic retrieval)",
        targetConfiguration={"mcp": {"connector": {
            "source": {"connectorId": "web-search", "version": s["gateway"]["web_search_connector_version"]},
            "configurations": [{"name": "WebSearch", "parameterValues": params}],
        }}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    for _ in range(60):
        t = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        if t["status"] == "READY":
            return t
        if t["status"].endswith("FAILED"):
            raise RuntimeError(f"web-search target failed: {t.get('statusReasons')}")
        time.sleep(5)
    return t


def ensure_rate_limit(s: Settings, gateway_id: str) -> dict | None:
    """Customer-defined per-caller limit (evaluated before service quotas). Idempotent by rateLimitId."""
    rpm = int(s["gateway"]["rate_limit_per_caller_rpm"] or 0)
    if rpm <= 0:
        return None
    ctl = _ctl(s)
    rl_id = "per-principal"
    try:
        return ctl.get_gateway_rate_limit(gatewayIdentifier=gateway_id, rateLimitId=rl_id)
    except ctl.exceptions.ResourceNotFoundException:
        pass
    return ctl.create_gateway_rate_limit(
        gatewayIdentifier=gateway_id, rateLimitId=rl_id,
        description="fair-share per IAM principal (dispatcher/worker identity)",
        dimensionKeys=["$.context.iam.principal"],
        entries=[{"dimensions": {"$.context.iam.principal": "*"},
                  "requests": [{"rate": float(rpm), "period": "minute"}]}],
    )
