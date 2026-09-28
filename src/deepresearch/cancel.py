"""Cancel a research task: mark it cancelled and stop the harness microVM session (StopRuntimeSession)."""
from __future__ import annotations

import time

from .aws_clients import client
from .config import Settings
from .task_store import TaskStore

TERMINAL = ("completed", "completed_no_report", "failed", "cancelled")


def cancel_task(settings: Settings, task_id: str, store: TaskStore | None = None, reason: str = "cancelled by client",
                subscription_id: str | None = None, force: bool = False) -> dict:
    """Cancel semantics with semantic merging:
    - subscription_id given: detach that frontend (its callback / stream). The backend research is stopped ONLY when no
      active subscription remains (or force=True).
    - no subscription_id: stop the backend only if the task has <=1 subscriber or force=True; otherwise refuse (merged task).
    """
    store = store or TaskStore(settings)
    t = store.get_task(task_id)
    if not t:
        return {"task_id": task_id, "error": "unknown task_id"}
    if t.get("status") in TERMINAL:
        return {"task_id": task_id, "status": t["status"], "note": "already terminal"}
    remaining = None
    if subscription_id:
        remaining = store.remove_subscription(task_id, subscription_id)
        if remaining > 0 and not force:
            return {"task_id": task_id, "status": t.get("status"), "unsubscribed": subscription_id, "remaining_subscribers": remaining,
                    "note": "other clients share this task; backend keeps running"}
    else:
        subs = store.list_subscriptions(task_id)
        if len(subs) > 1 and not force:
            return {"task_id": task_id, "status": t.get("status"), "remaining_subscribers": len(subs),
                    "note": "task is shared by several requests; pass subscription_id to detach yours, or force=true to stop for all"}
    prev = t.get("status")
    store.update_task(task_id, status="cancelled", cancel_reason=reason, cancelled_at=int(time.time()), cancelled_from=prev)
    stopped = None
    sid, arn = t.get("session_id"), t.get("harness_arn") or settings.state("harness_arn")
    if sid and arn and prev == "running":
        try:
            client("bedrock-agentcore", settings.harness_region).stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sid)
            stopped = True
        except Exception as e:  # noqa: BLE001
            stopped = f"stop_runtime_session failed: {e}"[:300]
    store.append_events(task_id, [{"kind": "cancelled", "text": reason}])
    return {"task_id": task_id, "status": "cancelled", "previous_status": prev, "session_stopped": stopped,
            "unsubscribed": subscription_id, "remaining_subscribers": remaining}
