from deepresearch.dedupe_core import CanonForm, merge_decision


def _f(topic, time=(None, None), ents=(), asp=()):
    return CanonForm(topic=topic, entities=list(ents), aspects=list(asp), time_from=time[0], time_to=time[1])


def test_exact_canonical_key_is_order_insensitive():
    a = _f("technology", ("2026-09-21", "2026-09-21"), asp=["news"], ents=["b", "a"])
    b = _f("technology", ("2026-09-21", "2026-09-21"), asp=["news"], ents=["a", "b"])
    assert a.exact_key() == b.exact_key()
    assert merge_decision(a, b, 0.5, 0.9, 0.68) == (True, "exact_canon")


def test_time_gate_blocks_different_scopes_even_if_identical_topic():
    a = _f("technology", ("2026-09-21", "2026-09-21"), asp=["news"])
    b = _f("technology", ("2026-08-22", "2026-09-21"), asp=["news"])
    assert merge_decision(a, b, 1.0, 0.9, 0.68) == (False, "time_gate")


def test_one_sided_time_goes_to_judge_never_auto_merges():
    a = _f("agentcore runtime v2 vs v1", ("2026-09-01", "2026-09-30"), asp=["changes"])
    b = _f("agentcore runtime v2 vs v1", asp=["changes"])
    assert merge_decision(a, b, 0.99, 0.9, 0.68, judge=None) == (False, "time_onesided")
    assert merge_decision(a, b, 0.99, 0.9, 0.68, judge=lambda: True) == (True, "judge_time")
    assert merge_decision(a, b, 0.5, 0.9, 0.68, judge=lambda: True) == (False, "time_onesided")  # too dissimilar for the judge


def test_similarity_bands():
    a, b = _f("quantum computing", asp=["news"]), _f("quantum computing developments", asp=["news"])
    assert merge_decision(a, b, 0.95, 0.9, 0.68) == (True, "sim_high")
    assert merge_decision(a, b, 0.50, 0.9, 0.68) == (False, "sim_low")
    assert merge_decision(a, b, 0.80, 0.9, 0.68, judge=lambda: False) == (False, "judge")
    assert merge_decision(a, b, 0.80, 0.9, 0.68, judge=lambda: True) == (True, "judge")
    assert merge_decision(a, b, 0.80, 0.9, 0.68) == (False, "grey_nojudge")


def test_service_judge_receives_original_queries(monkeypatch):
    """Regression: the service passed the canonical JSON string to the judge instead of the user's request."""
    import deepresearch.dedupe as dd
    seen = {}

    def fake_judge(rt, a, b, fa=None, fb=None):
        seen["a"], seen["b"] = a, b
        return True

    class R:  # minimal Valkey fake
        def __init__(self): self.h = {"{dr:dd}:task:t1": {b"canon": b'{"topic": "x", "entities": [], "aspects": ["news"], "time_from": null, "time_to": null}',
                                                          b"embed": dd._pack([1.0, 0.0]), b"query": "最近一周 X 有什么大新闻".encode()}}
        def get(self, k): return None
        def zrevrange(self, k, a, b): return [b"t1"]
        def hgetall(self, k): return self.h.get(k, {})
        def zrem(self, *a): pass

    svc = dd.DedupeService.__new__(dd.DedupeService)
    svc.cfg = {"max_candidates": 10, "similarity_high": 0.99, "similarity_low": 0.1}
    svc.r, svc.rt = R(), None
    svc._alive = lambda tid: True
    monkeypatch.setattr(dd, "judge_same", fake_judge)
    form = CanonForm(topic="x", aspects=["developments"], raw='{"topic":"x"}')
    out = svc._find(form, vec=[0.8, 0.6], query="过去 7 天 X 领域的重要进展")
    assert out["hit"] and seen == {"a": "过去 7 天 X 领域的重要进展", "b": "最近一周 X 有什么大新闻"}
