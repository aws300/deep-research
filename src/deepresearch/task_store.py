"""Task state + progress events + admission counters on the reused DynamoDB table (pk/sk, ttl)."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Iterable

from botocore.exceptions import ClientError

from .aws_clients import resource
from .config import Settings

COUNTER_PK = "DR#COUNTER"


def _now() -> int:
    return int(time.time())


def _py(o: Any) -> Any:
    if isinstance(o, list):
        return [_py(x) for x in o]
    if isinstance(o, dict):
        return {k: _py(v) for k, v in o.items()}
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    return o


class TaskStore:
    def __init__(self, settings: Settings, table=None):
        self.s = settings
        self.ttl_seconds = int(settings["table"]["ttl_days"]) * 86400
        self.table = table if table is not None else resource("dynamodb", settings.table_region).Table(settings.table_name)

    # ------------------------------------------------------------------ tasks
    def create_task(self, query: str, *, depth: str = "standard", actor_id: str = "anonymous",
                    metadata: dict | None = None) -> dict:
        task_id = f"t{int(time.time())}-{uuid.uuid4().hex[:10]}"
        item = {"pk": f"DR#TASK#{task_id}", "sk": "meta", "task_id": task_id, "query": query, "depth": depth,
                "actor_id": actor_id, "status": "queued", "created_at": _now(), "updated_at": _now(),
                "attempts": 0, "metadata": metadata or {}, "ttl": _now() + self.ttl_seconds}
        self.table.put_item(Item=item)
        return _py(item)

    def get_task(self, task_id: str) -> dict | None:
        r = self.table.get_item(Key={"pk": f"DR#TASK#{task_id}", "sk": "meta"})
        return _py(r.get("Item"))

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        self.table.update_item(Key={"pk": f"DR#TASK#{task_id}", "sk": "meta"}, UpdateExpression=expr,
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def claim_task(self, task_id: str, worker: str) -> bool:
        """Transition queued->running exactly once (idempotent redeliveries lose)."""
        try:
            self.table.update_item(
                Key={"pk": f"DR#TASK#{task_id}", "sk": "meta"},
                UpdateExpression="SET #s = :run, worker = :w, started_at = :t, updated_at = :t ADD attempts :one",
                ConditionExpression="#s IN (:q, :retry)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":run": "running", ":w": worker, ":t": _now(), ":one": 1,
                                           ":q": "queued", ":retry": "retry"})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ----------------------------------------------------------------- events
    def append_events(self, task_id: str, events: Iterable[dict]) -> int:
        n = 0
        with self.table.batch_writer() as bw:
            for ev in events:
                n += 1
                sk = f"evt#{_now()}#{uuid.uuid4().hex[:6]}"
                bw.put_item(Item={"pk": f"DR#TASK#{task_id}", "sk": sk, "ttl": _now() + self.ttl_seconds, **ev})
        return n

    def list_events(self, task_id: str, limit: int = 500) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        r = self.table.query(KeyConditionExpression=Key("pk").eq(f"DR#TASK#{task_id}") & Key("sk").begins_with("evt#"),
                             Limit=limit)
        return _py(r.get("Items", []))

    # ----------------------------------------------------------- subscriptions (frontends attached to one task)
    def add_subscription(self, task_id: str, *, actor_id: str = "anonymous", callback_url: str | None = None,
                         source: str = "sdk", query: str | None = None) -> dict:
        sub_id = f"s{uuid.uuid4().hex[:12]}"
        item = {"pk": f"DR#TASK#{task_id}", "sk": f"sub#{sub_id}", "subscription_id": sub_id, "task_id": task_id, "actor_id": actor_id,
                "callback_url": callback_url or "", "source": source, "query": (query or "")[:1000], "created_at": _now(),
                "ttl": _now() + self.ttl_seconds, "active": True}
        self.table.put_item(Item=item)
        self.table.update_item(Key={"pk": f"DR#TASK#{task_id}", "sk": "meta"}, UpdateExpression="ADD subscribers :one SET updated_at = :t",
                               ExpressionAttributeValues={":one": 1, ":t": _now()})
        return _py(item)

    def list_subscriptions(self, task_id: str, active_only: bool = True) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        r = self.table.query(KeyConditionExpression=Key("pk").eq(f"DR#TASK#{task_id}") & Key("sk").begins_with("sub#"))
        subs = _py(r.get("Items", []))
        return [s for s in subs if s.get("active", True)] if active_only else subs

    def remove_subscription(self, task_id: str, subscription_id: str) -> int:
        """Deactivate one subscription; returns the number of remaining active subscriptions."""
        try:
            self.table.update_item(Key={"pk": f"DR#TASK#{task_id}", "sk": f"sub#{subscription_id}"},
                                   UpdateExpression="SET active = :f, cancelled_at = :t", ConditionExpression="active = :tr",
                                   ExpressionAttributeValues={":f": False, ":t": _now(), ":tr": True})
            self.table.update_item(Key={"pk": f"DR#TASK#{task_id}", "sk": "meta"}, UpdateExpression="ADD subscribers :neg SET updated_at = :t",
                                   ExpressionAttributeValues={":neg": -1, ":t": _now()})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return len(self.list_subscriptions(task_id))

    def reclaim_stale_running(self, older_than_seconds: int = 300) -> list[str]:
        """Mark running tasks whose progress stopped (worker died) as `retry` so redelivered SQS messages can claim them."""
        cutoff = _now() - older_than_seconds
        r = self.table.scan(FilterExpression="begins_with(pk, :p) AND sk = :m AND #s = :run AND updated_at < :c",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":p": "DR#TASK#", ":m": "meta", ":run": "running", ":c": cutoff})
        ids = []
        for it in r.get("Items", []):
            self.update_task(it["task_id"], status="retry", last_error="worker lost (dispatcher restart or crash); requeued")
            self.append_events(it["task_id"], [{"kind": "system", "text": "worker lost; task marked retry"}])
            ids.append(it["task_id"])
        return ids

    def count_running(self) -> int:
        r = self.table.scan(FilterExpression="begins_with(pk, :p) AND sk = :m AND #s = :run", ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":p": "DR#TASK#", ":m": "meta", ":run": "running"}, Select="COUNT")
        return int(r.get("Count", 0))

    # --------------------------------------------------------------- counters
    def acquire_slot(self, limit: int, shard: str = "default") -> bool:
        """Atomically increment the in-flight counter if below limit."""
        try:
            self.table.update_item(
                Key={"pk": COUNTER_PK, "sk": f"inflight#{shard}"},
                UpdateExpression="ADD n :one SET updated_at = :t",
                ConditionExpression="attribute_not_exists(n) OR n < :lim",
                ExpressionAttributeValues={":one": 1, ":lim": limit, ":t": _now()})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release_slot(self, shard: str = "default") -> None:
        try:
            self.table.update_item(Key={"pk": COUNTER_PK, "sk": f"inflight#{shard}"},
                                   UpdateExpression="ADD n :neg SET updated_at = :t",
                                   ConditionExpression="n > :zero",
                                   ExpressionAttributeValues={":neg": -1, ":zero": 0, ":t": _now()})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def inflight(self, shard: str = "default") -> int:
        r = self.table.get_item(Key={"pk": COUNTER_PK, "sk": f"inflight#{shard}"})
        return int(r.get("Item", {}).get("n", 0))

    def reset_slots(self, shard: str = "default") -> None:
        self.table.put_item(Item={"pk": COUNTER_PK, "sk": f"inflight#{shard}", "n": 0, "updated_at": _now()})
