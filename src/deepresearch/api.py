"""SDK-level entry points (no public endpoint): submit a research task, read status/events, fetch report."""
from __future__ import annotations

import time
from typing import Iterator

from .aws_clients import client
from .config import Settings, load_settings
from .dedupe import DedupeClient
from .queue import IntakeQueue
from .task_store import TaskStore


class DeepResearchAPI:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or load_settings()
        self.store = TaskStore(self.s)
        self.queue = IntakeQueue(self.s)
        self.dedupe = DedupeClient(self.s, self.store, self.queue)

    def submit(self, query: str, depth: str = "standard", actor_id: str = "anonymous", metadata: dict | None = None,
               dedupe: bool = True, cache: bool | None = None) -> dict:
        """Submit (async). With cache=True (default) equivalent requests within the cache TTL share one task_id (merged=True)."""
        cb = (metadata or {}).get("callback_url")
        if cache is not None:
            dedupe = cache
        if dedupe:
            res = self.dedupe.resolve(query=query, depth=depth, actor_id=actor_id, callback_url=cb, source="sdk")
            task = self.store.get_task(res["task_id"]) or {"task_id": res["task_id"]}
            task.update(merged=res["merged"], subscription_id=res["subscription_id"], canonical=res.get("canonical"), dedupe_reason=res.get("reason"))
            return task
        task = self.store.create_task(query, depth=depth, actor_id=actor_id, metadata=metadata)
        sub = self.store.add_subscription(task["task_id"], actor_id=actor_id, callback_url=cb, source="sdk", query=query)
        task["message_id"] = self.queue.send(task)
        task.update(merged=False, subscription_id=sub["subscription_id"])
        return task

    def cancel(self, task_id: str, subscription_id: str | None = None, force: bool = False) -> dict:
        from .cancel import cancel_task
        return cancel_task(self.s, task_id, self.store, "api.cancel", subscription_id, force)

    def get(self, task_id: str) -> dict | None:
        return self.store.get_task(task_id)

    def events(self, task_id: str) -> list[dict]:
        return self.store.list_events(task_id)

    def follow(self, task_id: str, poll: float = 5.0, timeout: float = 3600) -> Iterator[dict]:
        """Yield new progress events until the task reaches a terminal state."""
        seen: set[str] = set()
        t0 = time.time()
        while time.time() - t0 < timeout:
            for ev in self.events(task_id):
                if ev["sk"] not in seen:
                    seen.add(ev["sk"])
                    yield ev
            t = self.get(task_id) or {}
            if t.get("status") in ("completed", "completed_no_report", "failed"):
                yield {"kind": "final", "status": t.get("status"), "result": t.get("result")}
                return
            time.sleep(poll)
        raise TimeoutError(task_id)

    def report(self, task_id: str) -> str | None:
        t = self.get(task_id) or {}
        uri = (t.get("result") or {}).get("report_s3")
        if not uri:
            return None
        bucket, key = uri[5:].split("/", 1)
        return client("s3", self.s.harness_region).get_object(Bucket=bucket, Key=key)["Body"].read().decode()

    def queue_depth(self) -> dict:
        d = self.queue.depth()
        d["executing"] = self.store.inflight()
        return d
