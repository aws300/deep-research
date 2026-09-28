"""Admission-controlled dispatcher: SQS -> token buckets -> harness worker threads.

Runs as a local process, a container on the existing EKS cluster (deploy/k8s/), or any long-lived host.
It never exposes a network endpoint.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Settings
from .queue import IntakeQueue
from .task_store import TaskStore
from .worker import ResearchWorker, ThrottledError

log = logging.getLogger("deepresearch.dispatcher")


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = float(rate_per_sec)
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def try_take(self, n: float = 1.0) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


class AdmissionController:
    """Four gates: in-flight slots (DynamoDB), session-creation rate, Web Search TPS budget, throttle backoff."""

    def __init__(self, settings: Settings, store: TaskStore, shard: str = "default"):
        d = settings["dispatcher"]
        self.store, self.shard = store, shard
        self.max_inflight = int(d["max_inflight"])
        self.session_bucket = TokenBucket(float(d["session_create_per_sec"]))
        # a task consumes searches over its lifetime; admit at the sustainable task rate implied by the search budget
        per_task_search_tps = float(d["searches_per_task_estimate"]) / (float(d["task_minutes_estimate"]) * 60)
        sustainable_tasks_per_sec = float(d["websearch_tps_budget"]) / max(per_task_search_tps, 1e-6) / (float(d["task_minutes_estimate"]) * 60)
        self.search_bucket = TokenBucket(sustainable_tasks_per_sec, burst=max(5.0, sustainable_tasks_per_sec * 60))
        self.backoff_until = 0.0
        self.lock = threading.Lock()

    def set_max_inflight(self, c: int) -> None:
        self.max_inflight = int(c)

    def admit(self) -> str | None:
        """Return None if admitted (slot acquired), else the reason for denial."""
        if time.monotonic() < self.backoff_until:
            return "throttle_backoff"
        if not self.session_bucket.try_take():
            return "session_rate"
        if not self.search_bucket.try_take():
            return "search_budget"
        if not self.store.acquire_slot(self.max_inflight, self.shard):
            return "inflight_limit"
        return None

    def release(self) -> None:
        self.store.release_slot(self.shard)

    def on_throttle(self, seconds: float = 30.0) -> None:
        with self.lock:
            self.backoff_until = max(self.backoff_until, time.monotonic() + seconds)
            self.max_inflight = max(1, int(self.max_inflight * 0.8))
            log.warning("throttled: backoff %.0fs, max_inflight now %d", seconds, self.max_inflight)


class Dispatcher:
    def __init__(self, settings: Settings, store: TaskStore | None = None, queue: IntakeQueue | None = None,
                 worker: ResearchWorker | None = None, shard: str = "default"):
        self.s = settings
        self.store = store or TaskStore(settings)
        self.queue = queue or IntakeQueue(settings)
        self.worker = worker or ResearchWorker(settings, self.store)
        self.admission = AdmissionController(settings, self.store, shard)
        self.pool = ThreadPoolExecutor(max_workers=int(settings["dispatcher"]["workers"]))
        self.visibility = int(settings["queue"]["visibility_timeout_seconds"])
        self._stop = threading.Event()
        self.stats = {"dispatched": 0, "denied": 0, "completed": 0, "failed": 0, "throttled": 0}

    # ------------------------------------------------------------------ loop
    def run_once(self, wait: int = 5) -> int:
        """Poll once; dispatch admitted tasks to the pool. Returns number dispatched."""
        n = 0
        # only pull as many messages as we can admit: denied messages would burn SQS receive counts and eventually
        # land in the DLQ (a real burst at 10k concurrency would otherwise drop tasks)
        free = self.admission.max_inflight - self.store.inflight(self.admission.shard)
        if free <= 0:
            time.sleep(min(wait, 5))
            return 0
        for msg in self.queue.receive(max_messages=max(1, min(10, int(self.s["dispatcher"]["workers"]), free)), wait=wait):
            reason = self.admission.admit()
            if reason:
                self.stats["denied"] += 1
                log.info("task %s denied admission: %s", msg["task_id"], reason)
                self.queue.release(msg["_receipt"], delay=5 if reason != "throttle_backoff" else 30)
                continue
            if not self.store.claim_task(msg["task_id"], worker=threading.current_thread().name):
                cur = self.store.get_task(msg["task_id"]) or {}
                if cur.get("status") == "running" and int(cur.get("updated_at", 0)) < int(time.time()) - 180:
                    # orphan: previous worker died mid-flight; take it over
                    log.warning("task %s looks orphaned (running, no progress for >180s) -> taking over", msg["task_id"])
                    self.store.update_task(msg["task_id"], status="retry", last_error="worker lost; taken over on redelivery")
                    if self.store.claim_task(msg["task_id"], worker=threading.current_thread().name):
                        self.pool.submit(self._execute, msg); self.stats["dispatched"] += 1; n += 1
                        continue
                # duplicate delivery or already handled (or cancelled while queued)
                log.info("task %s not claimable (status=%s, receive_count=%s) -> message dropped", msg["task_id"],
                         (self.store.get_task(msg["task_id"]) or {}).get("status"), msg.get("_receive_count"))
                self.admission.release()
                self.queue.delete(msg["_receipt"])
                continue
            log.info("task %s dispatched (inflight=%d/%d, receive_count=%s)", msg["task_id"], self.store.inflight(self.admission.shard),
                     self.admission.max_inflight, msg.get("_receive_count"))
            self.pool.submit(self._execute, msg)
            self.stats["dispatched"] += 1
            n += 1
        return n

    def reconcile(self, stale_seconds: int = 300) -> None:
        """Called at startup: requeue tasks orphaned by a previous dispatcher and resync the in-flight counter."""
        stale = self.store.reclaim_stale_running(stale_seconds)
        for tid in stale:
            t = self.store.get_task(tid) or {}
            self.queue.send({"task_id": tid, "query": t.get("query", ""), "depth": t.get("depth", "standard"), "actor_id": t.get("actor_id", "anonymous")})
            log.warning("reconcile: task %s was orphaned -> retry + requeued", tid)
        redriven = self.redrive_dlq()
        running = self.store.count_running()
        self.store.reset_slots(self.admission.shard)
        for _ in range(running):
            self.store.acquire_slot(10**9, self.admission.shard)
        log.info("reconcile: %d orphaned task(s) requeued, inflight counter resynced to %d running task(s)", len(stale), running)

    def redrive_dlq(self, max_messages: int = 500) -> int:
        """Move DLQ messages whose task is still unfinished back to the intake queue (called on startup)."""
        dlq_url = self.s.state("dlq_url")
        if not dlq_url:
            return 0
        sqs, moved, seen = self.queue.sqs, 0, 0
        while seen < max_messages:
            msgs = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])
            if not msgs:
                break
            for m in msgs:
                seen += 1
                body = json.loads(m["Body"])
                status = (self.store.get_task(body.get("task_id", "")) or {}).get("status")
                if status in ("queued", "retry"):
                    self.queue.sqs.send_message(QueueUrl=self.queue.url, MessageBody=m["Body"])
                    moved += 1
                    log.warning("redrive: task %s (status=%s) moved from DLQ back to intake", body.get("task_id"), status)
                sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
        if seen:
            log.info("redrive: inspected %d DLQ message(s), requeued %d unfinished task(s)", seen, moved)
        return moved

    def _watchdog(self, stale_seconds: int | None = None) -> None:
        """Running tasks with no progress for `stale_seconds` are marked retry and requeued; the stuck worker thread's
        late writes are ignored because the task is re-claimed under a new session."""
        stale_seconds = stale_seconds or int(os.environ.get("DR_WATCHDOG_STALE_SECONDS", "480"))
        try:
            for tid in self.store.reclaim_stale_running(stale_seconds):
                t = self.store.get_task(tid) or {}
                self.queue.send({"task_id": tid, "query": t.get("query", ""), "depth": t.get("depth", "standard"),
                                 "actor_id": t.get("actor_id", "anonymous")})
                self.admission.release()
                log.warning("watchdog: task %s had no progress for >%ss -> retry + requeued", tid, stale_seconds)
        except Exception:  # noqa: BLE001
            log.exception("watchdog failed")

    def run_forever(self) -> None:
        if os.environ.get("DR_RECONCILE_ON_START", "1") == "1":
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("reconcile failed")
        log.info("dispatcher started: max_inflight=%d workers=%d queue=%s table=%s harness=%s", self.admission.max_inflight,
                 self.pool._max_workers, self.queue.url, self.store.s.table_name, self.worker.harness.arn)
        last_hb = 0.0
        while not self._stop.is_set():
            try:
                self.run_once()
                if time.time() - last_hb > 60:
                    log.info("heartbeat stats=%s queue=%s inflight=%d", self.stats, self.queue.depth(), self.store.inflight(self.admission.shard))
                    last_hb = time.time()
                    self._watchdog()
            except Exception:  # noqa: BLE001
                log.exception("dispatcher loop error")
                time.sleep(5)

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=True)

    # --------------------------------------------------------------- execute
    def _execute(self, msg: dict) -> None:
        receipt = msg.pop("_receipt")
        heartbeat = threading.Event()

        def keepalive():
            while not heartbeat.wait(self.visibility * 0.6):
                try:
                    self.queue.extend(receipt, self.visibility)
                except Exception:  # noqa: BLE001
                    pass

        hb = threading.Thread(target=keepalive, daemon=True)
        hb.start()
        try:
            self.worker.run(msg)
            self.queue.delete(receipt)
            self.stats["completed"] += 1
        except ThrottledError as e:
            self.stats["throttled"] += 1
            log.warning("task %s throttled -> retry in 30s: %s", msg["task_id"], str(e)[:200])
            self.admission.on_throttle()
            self.store.update_task(msg["task_id"], status="retry", last_error=str(e)[:500])
            self.queue.release(receipt, delay=30)
        except Exception as e:  # noqa: BLE001
            if (self.store.get_task(msg["task_id"]) or {}).get("status") == "cancelled":
                self.queue.delete(receipt)
                return
            self.stats["failed"] += 1
            log.exception("task %s failed", msg["task_id"])
            self.store.update_task(msg["task_id"], status="failed", last_error=str(e)[:500])
            self.queue.delete(receipt)
            try:
                self.worker.notify(self.store.get_task(msg["task_id"]) or msg, "failed", None, error=str(e)[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            heartbeat.set()
            self.admission.release()
