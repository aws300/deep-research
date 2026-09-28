"""VPC Lambda handler exposing DedupeService (the only component allowed to talk to Valkey from outside the VPC)."""
from __future__ import annotations

import json
import logging
import os

from .config import load_settings
from .dedupe import DedupeService

log = logging.getLogger("deepresearch.dedupe_lambda")
log.setLevel(os.environ.get("DR_LOG_LEVEL", "INFO"))
_S = load_settings()
_SVC: DedupeService | None = None


def _svc() -> DedupeService:
    global _SVC
    if _SVC is None:
        _SVC = DedupeService(_S)
    return _SVC


ALLOWED = {"resolve": {"query", "depth", "actor_id", "callback_url", "source"}, "lookup": {"query"}, "stats": set(), "clear": set()}


def lambda_handler(event, context):
    action = event.get("action", "resolve")
    # ADOT/OTel botocore instrumentation injects a `headers` field (trace context) into Lambda payloads -> whitelist params
    kw = {k: v for k, v in event.items() if k in ALLOWED.get(action, set())}
    log.info("action=%s args=%s", action, json.dumps({k: str(v)[:120] for k, v in kw.items()}, ensure_ascii=False))
    try:
        if action == "resolve":
            return _svc().resolve(**kw)
        if action == "lookup":
            out = _svc().lookup(kw["query"]); out.pop("vec", None); return out
        if action == "stats":
            return _svc().stats()
        if action == "clear":
            return {"cleared": _svc().clear()}
        return {"error": f"unknown action {action}"}
    except Exception as e:  # noqa: BLE001
        log.exception("dedupe action failed")
        return {"error": f"{type(e).__name__}: {e}"}
