from deepresearch.capacity import Quotas, TaskProfile, max_inflight, quotas_for_target


def test_default_quotas_bound_by_model_tpm_or_search():
    r = max_inflight(Quotas(), TaskProfile())
    assert r["binding_constraint"] in ("model_tpm", "web_search")
    assert 50 <= r["C"] <= 400


def test_raising_quotas_raises_c():
    base = max_inflight(Quotas(), TaskProfile())["C"]
    big = max_inflight(Quotas(websearch_tps=300, model_tpm=60_000_000, runtime_active_sessions=12000,
                              gateway_toolcall_tps=1000, gateway_connections=12000), TaskProfile())
    assert big["C"] > base * 10
    assert big["C"] <= 12000 * 0.8


def test_cache_and_burndown_change_tpm():
    p_hot = TaskProfile(cache_hit_ratio=0.5, output_burndown=1.0)
    p_cold = TaskProfile(cache_hit_ratio=0.0, output_burndown=10.0)
    assert p_hot.tpm < p_cold.tpm


def test_inverse_targets_10000():
    need = quotas_for_target(10000, TaskProfile(), shards=1)
    assert need["runtime_active_sessions"] >= 10000
    assert need["websearch_tps"] > 100
    assert need["model_tpm"] > 6_000_000


def test_ramp_time_uses_session_rate():
    r = max_inflight(Quotas(session_create_tps=25), TaskProfile())
    assert abs(r["ramp_seconds_to_C"] - r["C"] / 25) < 0.1
