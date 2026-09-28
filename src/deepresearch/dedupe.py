"""Semantic merge of equivalent async research requests -> one task_id (Valkey-backed, 12 h TTL by default).

Pipeline (see scripts/validate_embed.py for the validation suite):
  canonicalize(query)  -> CanonForm                       (Haiku, temperature 0, absolute dates)
  exact key lookup     -> task_id                          (byte-identical canonical descriptor)
  else embed + scan candidates in the same time-scope bucket -> cosine -> HIGH merge / LOW distinct / grey -> LLM judge
  miss -> 5 s merge lock (SET NX PX) so concurrent identical requests wait and receive the same task_id
Every request (hit or miss) registers a *subscription* on the task, so cancellation can detach one frontend without
stopping the shared backend research. Any Valkey/Bedrock failure fails OPEN: a fresh task is created (logged).

Key layout (single hash slot via {dr:dd} tag; Valkey serverless runs in cluster mode):
  {dr:dd}:exact:<sha256(exact_key)>   -> task_id                       TTL
  {dr:dd}:task:<task_id>              -> hash {canon, embed(bytes), query, created}  TTL
  {dr:dd}:bucket:<time_from>_<time_to>-> ZSET task_id by created_ts     TTL (refreshed)
  {dr:dd}:lock:<sha>                  -> owner, PX window_seconds
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import time
import uuid

from .aws_clients import client
from .config import Settings
from .dedupe_core import CanonForm, canonicalize, cosine, embed_texts, judge_same, merge_decision
from .queue import IntakeQueue
from .task_store import TaskStore

log = logging.getLogger("deepresearch.dedupe")
PFX = "{dr:dd}"


class DedupeService:
    """Runs where Valkey is reachable (VPC Lambda, or any host inside the VPC)."""

    def __init__(self, settings: Settings, store: TaskStore | None = None, queue: IntakeQueue | None = None, valkey=None):
        self.s = settings
        self.cfg = settings["dedupe"]
        self.store = store or TaskStore(settings)
        self.queue = queue or IntakeQueue(settings)
        self.rt = client("bedrock-runtime", settings.harness_region)
        self.r = valkey or self._connect()
        self.ttl = int(os.environ.get("DR_DEDUPE_TTL_SECONDS", self.cfg["ttl_seconds"]))
        self.window = float(self.cfg["window_seconds"])
        self.dims = int(self.cfg["embed_dims"])

    def _connect(self):
        import ssl
        import redis
        sec = json.loads(client("secretsmanager", self.s.region).get_secret_value(SecretId=self.cfg["valkey_secret_id"])["SecretString"])
        pwd = sec.get("password") or None          # ElastiCache Serverless without a user group -> no AUTH (TLS + SG only)
        return redis.Redis(host=sec["host"], port=int(sec.get("port", 6379)), username=(sec.get("username") or "default") if pwd else None,
                           password=pwd, ssl=str(sec.get("tls", True)).lower() not in ("false", "0"), ssl_cert_reqs=ssl.CERT_REQUIRED,
                           socket_timeout=3, socket_connect_timeout=3, health_check_interval=30)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _sha(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()[:32]

    @staticmethod
    def _bucket(f: CanonForm) -> str:
        return f"{PFX}:bucket:{f.time_from or 'none'}_{f.time_to or 'none'}"

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------ lookup
    def lookup(self, query: str) -> dict:
        """Find an equivalent live task without creating anything."""
        form = canonicalize(self.rt, query, self._today())
        hit = self._find(form, query=query)
        return {"query": query, "canonical": form.as_dict(), **hit}

    def _find(self, form: CanonForm, vec: list[float] | None = None, query: str = "") -> dict:
        exact = self.r.get(f"{PFX}:exact:{self._sha(form.exact_key())}")
        if exact:
            tid = exact.decode()
            if self._alive(tid):
                return {"hit": True, "task_id": tid, "reason": "exact_canon", "similarity": 1.0}
        if vec is None:
            vec = embed_texts(self.rt, [form.embed_text()], self.dims)[0]
        cands = self.r.zrevrange(self._bucket(form), 0, int(self.cfg["max_candidates"]) - 1)
        best = None
        for tid_b in cands:
            tid = tid_b.decode()
            h = self.r.hgetall(f"{PFX}:task:{tid}")
            if not h or not self._alive(tid):
                self.r.zrem(self._bucket(form), tid)
                continue
            other = CanonForm(**json.loads(h[b"canon"].decode()))
            sim = cosine(vec, _unpack(h[b"embed"]))
            if best is None or sim > best[1]:
                best = (tid, sim, other, h.get(b"query", b"").decode())
        if best is None:
            return {"hit": False, "reason": "no_candidates", "vec": vec}
        tid, sim, other, other_q = best
        # the judge must compare the two ORIGINAL requests (plus descriptors), exactly like scripts/validate_embed.py
        merge, why = merge_decision(form, other, sim, float(self.cfg["similarity_high"]), float(self.cfg["similarity_low"]),
                                    lambda: judge_same(self.rt, query or form.topic, other_q or other.topic, form, other))
        return {"hit": merge, "task_id": tid if merge else None, "reason": why, "similarity": round(sim, 4), "vec": vec}

    def _alive(self, task_id: str) -> bool:
        t = self.store.get_task(task_id)
        return bool(t) and t.get("status") not in ("failed", "cancelled")

    # ------------------------------------------------------------------ resolve
    def resolve(self, query: str, depth: str = "standard", actor_id: str = "anonymous", callback_url: str | None = None,
                source: str = "sdk") -> dict:
        """Return {task_id, merged, subscription_id, canonical, reason}. Creates the task on a miss."""
        try:
            form = canonicalize(self.rt, query, self._today())
        except Exception as e:  # noqa: BLE001  fail open
            log.warning("canonicalize failed (%s) -> no dedupe", e)
            return self._create(query, depth, actor_id, callback_url, source, None, "canon_error")
        sha = self._sha(form.exact_key())
        lock_key, token = f"{PFX}:lock:{sha}", uuid.uuid4().hex
        deadline = time.time() + self.window
        try:
            while True:
                found = self._find(form, query=query)
                if found["hit"]:
                    sub = self.store.add_subscription(found["task_id"], actor_id=actor_id, callback_url=callback_url, source=source, query=query)
                    log.info("MERGE query=%r -> task=%s reason=%s sim=%s", query[:80], found["task_id"], found["reason"], found.get("similarity"))
                    return {"task_id": found["task_id"], "merged": True, "subscription_id": sub["subscription_id"],
                            "canonical": form.as_dict(), "reason": found["reason"], "similarity": found.get("similarity")}
                if self.r.set(lock_key, token, nx=True, px=int(self.window * 1000)):
                    try:
                        res = self._create(query, depth, actor_id, callback_url, source, form, found["reason"])
                        self._remember(form, res["task_id"], query, found.get("vec"))
                        return res
                    finally:
                        if self.r.get(lock_key) == token.encode():
                            self.r.delete(lock_key)
                if time.time() >= deadline:      # lock holder is slow; do not block the caller further
                    log.warning("merge window expired for %s; creating a separate task", sha)
                    return self._create(query, depth, actor_id, callback_url, source, form, "window_expired")
                time.sleep(0.2)                  # another identical request is creating the task -> re-check
        except Exception as e:  # noqa: BLE001  fail open
            log.warning("dedupe failed (%s) -> creating task without merge", e)
            return self._create(query, depth, actor_id, callback_url, source, form, "dedupe_error")

    def _create(self, query, depth, actor_id, callback_url, source, form: CanonForm | None, reason: str) -> dict:
        meta = {"source": source}
        if callback_url:
            meta["callback_url"] = callback_url
        if form:
            meta["canonical"] = form.as_dict()
        task = self.store.create_task(query, depth=depth, actor_id=actor_id, metadata=meta)
        sub = self.store.add_subscription(task["task_id"], actor_id=actor_id, callback_url=callback_url, source=source, query=query)
        self.queue.send(task)
        log.info("CREATE task=%s depth=%s reason=%s query=%r", task["task_id"], depth, reason, query[:80])
        return {"task_id": task["task_id"], "merged": False, "subscription_id": sub["subscription_id"],
                "canonical": form.as_dict() if form else None, "reason": reason}

    def _remember(self, form: CanonForm, task_id: str, query: str, vec: list[float] | None) -> None:
        if vec is None:
            vec = embed_texts(self.rt, [form.embed_text()], self.dims)[0]
        p = self.r.pipeline()
        p.set(f"{PFX}:exact:{self._sha(form.exact_key())}", task_id, ex=self.ttl)
        p.hset(f"{PFX}:task:{task_id}", mapping={"canon": json.dumps(form.as_dict(), ensure_ascii=False), "embed": _pack(vec),
                                                  "query": query[:1000], "created": int(time.time())})
        p.expire(f"{PFX}:task:{task_id}", self.ttl)
        p.zadd(self._bucket(form), {task_id: time.time()})
        p.expire(self._bucket(form), self.ttl)
        p.execute()

    # ------------------------------------------------------------------ ops
    def stats(self) -> dict:
        keys = list(self.r.scan_iter(match=f"{PFX}:*", count=500))
        kinds: dict[str, int] = {}
        for k in keys:
            kinds[k.decode().split(":")[2]] = kinds.get(k.decode().split(":")[2], 0) + 1
        return {"keys": len(keys), "by_kind": kinds, "ttl_seconds": self.ttl, "window_seconds": self.window,
                "thresholds": {"high": self.cfg["similarity_high"], "low": self.cfg["similarity_low"]}, "dims": self.dims}

    def clear(self) -> int:
        n = 0
        for k in self.r.scan_iter(match=f"{PFX}:*", count=500):
            self.r.delete(k); n += 1
        log.warning("dedupe cache cleared: %d keys", n)
        return n


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


# ---------------------------------------------------------------------- client used by every submit path
class DedupeClient:
    """Chooses direct Valkey (inside the VPC) or the VPC Lambda (everywhere else). Falls back to plain task creation."""

    def __init__(self, settings: Settings, store: TaskStore | None = None, queue: IntakeQueue | None = None):
        self.s = settings
        self.cfg = settings["dedupe"]
        self.store = store or TaskStore(settings)
        self.queue = queue or IntakeQueue(settings)
        self.mode = os.environ.get("DR_DEDUPE_MODE", self.cfg.get("mode", "auto"))
        self._svc: DedupeService | None = None

    def _direct_ok(self) -> bool:
        try:
            sec = json.loads(client("secretsmanager", self.s.region).get_secret_value(SecretId=self.cfg["valkey_secret_id"])["SecretString"])
            with socket.create_connection((sec["host"], int(sec.get("port", 6379))), timeout=1.0):
                return True
        except Exception:  # noqa: BLE001
            return False

    def _service(self) -> DedupeService | None:
        if self._svc is None and (self.mode == "direct" or (self.mode == "auto" and self._direct_ok())):
            self._svc = DedupeService(self.s, self.store, self.queue)
        return self._svc

    def call(self, action: str, **kw) -> dict:
        if not self.cfg.get("enabled", True):
            return self._plain(**kw) if action == "resolve" else {"error": "dedupe disabled"}
        svc = self._service()
        if svc is not None:
            return getattr(svc, action)(**kw)
        if self.mode in ("auto", "lambda"):
            try:
                lam = client("lambda", self.s.region)
                r = lam.invoke(FunctionName=self.cfg["lambda_name"], Payload=json.dumps({"action": action, **kw}).encode())
                out = json.loads(r["Payload"].read())
                if "errorMessage" in out:
                    raise RuntimeError(out["errorMessage"])
                return out
            except Exception as e:  # noqa: BLE001
                log.warning("dedupe lambda unavailable (%s) -> plain create", e)
        return self._plain(**kw) if action == "resolve" else {"error": "dedupe backend unreachable"}

    def _plain(self, query: str, depth: str = "standard", actor_id: str = "anonymous", callback_url: str | None = None, source: str = "sdk") -> dict:
        meta = {"source": source}
        if callback_url:
            meta["callback_url"] = callback_url
        task = self.store.create_task(query, depth=depth, actor_id=actor_id, metadata=meta)
        sub = self.store.add_subscription(task["task_id"], actor_id=actor_id, callback_url=callback_url, source=source, query=query)
        self.queue.send(task)
        return {"task_id": task["task_id"], "merged": False, "subscription_id": sub["subscription_id"], "canonical": None, "reason": "dedupe_off"}

    def resolve(self, cache: bool = True, **kw) -> dict:
        """cache=False bypasses the semantic-merge lookup AND does not register the new task for later merges."""
        if not cache:
            out = self._plain(**kw); out["reason"] = "cache_off"; return out
        return self.call("resolve", **kw)
