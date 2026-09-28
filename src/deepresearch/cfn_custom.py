"""CloudFormation custom resources that need the vendored (current) boto3.

  Action=harness_thinking   apply extended thinking to the harness model with an *integer* budget_tokens.
                            AWS::BedrockAgentCore::Harness passes Model.BedrockModelConfig.AdditionalParams through
                            CloudFormation, which turns every scalar into a string ("3000"); the model API rejects that.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)


def _send(event: dict, context, status: str, pid: str, data: dict | None = None, reason: str | None = None) -> None:
    body = json.dumps({"Status": status, "Reason": reason or f"see {context.log_stream_name}", "PhysicalResourceId": pid,
                       "StackId": event["StackId"], "RequestId": event["RequestId"],
                       "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}}).encode()
    urllib.request.urlopen(urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                                  headers={"Content-Type": "", "Content-Length": str(len(body))}), timeout=60)


def harness_thinking(harness_id: str, budget: int) -> dict:
    ctl = boto3.client("bedrock-agentcore-control")
    h = ctl.get_harness(harnessId=harness_id)["harness"]
    bm = dict(h["model"]["bedrockModelConfig"])
    if budget > 0:
        bm["additionalParams"] = {"additionalModelRequestFields": {"thinking": {"type": "enabled", "budget_tokens": budget}}}
    else:
        bm.pop("additionalParams", None)
    if bm == h["model"]["bedrockModelConfig"]:
        return {"Changed": "false"}
    ctl.update_harness(harnessId=harness_id, model={"bedrockModelConfig": bm})
    for _ in range(90):
        st = ctl.get_harness(harnessId=harness_id)["harness"]["status"]
        if st == "READY":
            return {"Changed": "true"}
        if st.endswith("FAILED"):
            raise RuntimeError(f"harness {harness_id} {st}")
        time.sleep(5)
    raise TimeoutError("harness not READY after update")


def lambda_handler(event, context):
    p, rt = event["ResourceProperties"], event["RequestType"]
    pid = event.get("PhysicalResourceId") or f"{p.get('Action')}-{p.get('HarnessId', '')}"
    try:
        data: dict = {}
        if rt != "Delete" and p["Action"] == "harness_thinking":
            data = harness_thinking(p["HarnessId"], int(p.get("BudgetTokens", "0")))
            log.info("harness_thinking %s budget=%s -> %s", p["HarnessId"], p.get("BudgetTokens"), data)
        _send(event, context, "SUCCESS", pid, data)
    except Exception as e:  # noqa: BLE001
        log.exception("custom resource failed")
        _send(event, context, "FAILED", pid, reason=str(e)[:900])
