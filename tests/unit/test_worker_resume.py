"""Worker keeps a task alive when the harness event stream breaks mid-turn (re-attaches to the same session)."""
import pytest
from botocore.exceptions import ResponseStreamingError

from deepresearch import worker as worker_mod
from deepresearch.worker import ResearchWorker as Worker


class FakeHarness:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def invoke(self, prompt, session_id, actor_id=None, overrides=None):
        self.calls.append((prompt, session_id))
        step = self.script.pop(0)
        for ev in step.get("events", []):
            yield ev
        if "raise" in step:
            raise step["raise"]


def _worker(harness):
    w = Worker.__new__(Worker)   # only _invoke_with_resume is exercised; no AWS clients needed
    w.harness = harness
    return w


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: None)


def test_stream_break_reattaches_same_session():
    h = FakeHarness([
        {"events": [{"type": "text", "text": "a"}], "raise": ResponseStreamingError(error="Response ended prematurely")},
        {"events": [{"type": "text", "text": "b"}, {"type": "stop", "reason": "end_turn"}]},
    ])
    evs = list(_worker(h)._invoke_with_resume("research X", "sess-1", "actor", {}))
    assert [c[1] for c in h.calls] == ["sess-1", "sess-1"]
    assert h.calls[1][0] == Worker.STREAM_RESUME
    assert any("[service] stream interrupted" in e.get("text", "") for e in evs)
    assert evs[-1] == {"type": "stop", "reason": "end_turn"}


def test_busy_previous_turn_is_retried_then_gives_up():
    class Conflict(Exception):
        pass
    busy = Conflict("ConflictException: session is currently processing a request")
    h = FakeHarness([{"raise": busy}] * 5)
    with pytest.raises(Conflict):
        list(_worker(h)._invoke_with_resume("q", "s", None, {}, max_reconnects=4))
    assert len(h.calls) == 5   # first attempt + 4 reconnects


def test_non_transient_errors_are_not_retried():
    h = FakeHarness([{"raise": ValueError("ValidationException: bad model id")}])
    with pytest.raises(ValueError):
        list(_worker(h)._invoke_with_resume("q", "s", None, {}))
    assert len(h.calls) == 1


def test_throttling_is_left_to_the_dispatcher():
    assert not Worker._is_transient(RuntimeError("ThrottlingException: too many concurrent sessions"))
    assert Worker._is_transient(ResponseStreamingError(error="Connection broken: IncompleteRead(0 bytes read)"))
