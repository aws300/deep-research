"""Thread-safe boto3 client factory with adaptive retries (AgentCore/Bedrock throttle with 429s).

boto3's default session is not safe for concurrent client creation from worker threads; every client is created under a
lock from one shared Session and cached. Long-lived streaming reads (InvokeHarness) use a long read timeout; everything else
uses short timeouts so a stuck socket can never hang a worker for an hour.
"""
from __future__ import annotations

import threading

import boto3
from botocore.config import Config

_LOCK = threading.Lock()
_SESSION = boto3.session.Session()
_CACHE: dict[tuple, object] = {}
_STREAM_CFG = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=3700, connect_timeout=10, max_pool_connections=64)
_SHORT_CFG = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=180, connect_timeout=10, max_pool_connections=64)


def client(service: str, region: str, *, long_read: bool | None = None, **kwargs):
    """Cached client. long_read defaults to True only for the AgentCore data plane (harness event streams)."""
    if long_read is None:
        long_read = service == "bedrock-agentcore"
    key = (service, region, long_read, tuple(sorted(kwargs.items())))
    c = _CACHE.get(key)
    if c is None:
        with _LOCK:
            c = _CACHE.get(key)
            if c is None:
                c = _SESSION.client(service, region_name=region, config=_STREAM_CFG if long_read else _SHORT_CFG, **kwargs)
                _CACHE[key] = c
    return c


def resource(service: str, region: str):
    key = ("resource", service, region)
    r = _CACHE.get(key)
    if r is None:
        with _LOCK:
            r = _CACHE.get(key)
            if r is None:
                r = _SESSION.resource(service, region_name=region, config=_SHORT_CFG)
                _CACHE[key] = r
    return r
