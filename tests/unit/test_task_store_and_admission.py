"""Unit tests with an in-memory fake of the DynamoDB Table interface (no AWS calls)."""
import time

from botocore.exceptions import ClientError

from deepresearch.config import load_settings
from deepresearch.dispatcher import AdmissionController, TokenBucket
from deepresearch.task_store import TaskStore


class FakeTable:
    def __init__(self):
        self.items = {}

    def _k(self, key):
        return (key["pk"], key["sk"])

    def put_item(self, Item):
        self.items[self._k(Item)] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(self._k(Key))
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None, ConditionExpression=None):
        it = self.items.setdefault(self._k(Key), dict(Key))
        names = ExpressionAttributeNames or {}
        v = ExpressionAttributeValues
        # minimal condition support for the expressions used in TaskStore
        if ConditionExpression:
            if "attribute_not_exists(n) OR n < :lim" in ConditionExpression and not ("n" not in it or it["n"] < v[":lim"]):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            if ConditionExpression == "n > :zero" and not it.get("n", 0) > 0:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            if "#s IN" in ConditionExpression and it.get("status") not in (v[":q"], v[":retry"]):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        expr = UpdateExpression
        if expr.startswith("ADD n"):
            it["n"] = it.get("n", 0) + list(v.values())[0]
            it["updated_at"] = v[":t"]
            return
        if "ADD attempts" in expr:
            it.update(status="running", worker=v[":w"], started_at=v[":t"], attempts=it.get("attempts", 0) + 1)
            return
        for part in expr.replace("SET ", "").split(","):
            lhs, rhs = [x.strip() for x in part.split("=")]
            it[names.get(lhs, lhs)] = v[rhs]

    class _BW:
        def __init__(self, t): self.t = t
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def put_item(self, Item): self.t.put_item(Item=Item)

    def batch_writer(self):
        return self._BW(self)

    def query(self, KeyConditionExpression, Limit):
        pk = KeyConditionExpression._values[0]._values[1]
        return {"Items": [dict(v) for (p, s), v in self.items.items() if p == pk and s.startswith("evt#")]}


def _store():
    return TaskStore(load_settings(), table=FakeTable())


def test_task_lifecycle_and_claim_once():
    st = _store()
    t = st.create_task("q", depth="quick", actor_id="u1")
    assert st.get_task(t["task_id"])["status"] == "queued"
    assert st.claim_task(t["task_id"], "w1") is True
    assert st.claim_task(t["task_id"], "w2") is False  # duplicate delivery loses
    st.update_task(t["task_id"], status="completed", result={"ok": 1})
    assert st.get_task(t["task_id"])["status"] == "completed"


def test_inflight_counter_respects_limit():
    st = _store()
    assert st.acquire_slot(2) and st.acquire_slot(2)
    assert st.acquire_slot(2) is False
    st.release_slot()
    assert st.inflight() == 1
    st.release_slot(); st.release_slot()  # extra release is a no-op
    assert st.inflight() == 0


def test_admission_controller_gates():
    s = load_settings()
    s.raw["dispatcher"]["max_inflight"] = 1
    s.raw["dispatcher"]["session_create_per_sec"] = 1000
    s.raw["dispatcher"]["websearch_tps_budget"] = 1000
    ac = AdmissionController(s, _store())
    assert ac.admit() is None
    assert ac.admit() == "inflight_limit"
    ac.release()
    ac.on_throttle(seconds=60)
    assert ac.admit() == "throttle_backoff"


def test_token_bucket():
    b = TokenBucket(rate_per_sec=1000, burst=2)
    assert b.try_take() and b.try_take()
    assert b.try_take() is False
    time.sleep(0.005)
    assert b.try_take()


def test_dispatcher_does_not_receive_when_no_free_slot():
    """Regression: denied messages burned SQS receive counts and ended in the DLQ. With no free slot, nothing is received."""
    from deepresearch.dispatcher import Dispatcher

    class Q:
        url = "q"
        def __init__(self): self.calls = []
        def receive(self, max_messages=1, wait=5): self.calls.append(max_messages); return []

    s = load_settings()
    s.raw["dispatcher"]["max_inflight"] = 2
    st = _store()
    q = Q()
    d = Dispatcher(s, store=st, queue=q, worker=object())
    st.acquire_slot(10); st.acquire_slot(10)
    assert d.run_once(wait=0) == 0 and q.calls == []          # full -> no receive at all
    st.release_slot()
    d.run_once(wait=0)
    assert q.calls == [1]                                       # one free slot -> ask for exactly one message
