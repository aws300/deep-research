"""Capacity math: how many research tasks may execute concurrently (C) given quotas and a task profile.

C = headroom * min(S_runtime, Q_search / r_search, Q_tpm / r_tpm, Q_gateway / r_gateway, Q_conn)
See docs/QUOTAS.md.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class TaskProfile:
    minutes: float = 20.0            # wall-clock per task
    searches: int = 30               # WebSearch calls per task
    model_calls: int = 40            # model round trips per task
    input_tokens_per_call: int = 8000
    output_tokens_per_call: int = 1500
    output_burndown: float = 5.0     # Claude 4.7 and below: 5x, Sonnet 5: 10x, 4.8: 15x, non-Claude: 1x
    cache_hit_ratio: float = 0.0     # fraction of input tokens served from prompt cache (not counted toward TPM)

    @property
    def search_tps(self) -> float:
        return self.searches / (self.minutes * 60)

    @property
    def tpm(self) -> float:
        calls_per_min = self.model_calls / self.minutes
        per_call = self.input_tokens_per_call * (1 - self.cache_hit_ratio) + self.output_tokens_per_call * self.output_burndown
        return calls_per_min * per_call

    @property
    def gateway_tps(self) -> float:
        # each search is one gateway tool call; add ~20% for tools/list & misc
        return self.search_tps * 1.2


@dataclass
class Quotas:
    runtime_active_sessions: int = 5000      # per account (us-east-1/us-west-2 default), not in Service Quotas console
    session_create_tps: float = 25.0         # L-8EE2AEA2
    websearch_tps: float = 10.0              # L-84A99A88 (us-east-1)
    gateway_toolcall_tps: float = 200.0      # L-A0D48779 / L-8CAB3FF3
    gateway_connections: int = 5000          # L-6234C8FD
    model_tpm: float = 6_000_000             # e.g. L-7BEE40FB Sonnet 4.6 global CRIS
    memory_create_event_tps: float = 200.0   # L-59AF2B24 (only if memory enabled)
    memory_events_per_task_min: float = 0.0  # ~2/min when harness memory is on


def max_inflight(q: Quotas, p: TaskProfile, headroom: float = 0.8) -> dict:
    limits = {
        "runtime_sessions": q.runtime_active_sessions,
        "web_search": q.websearch_tps / p.search_tps,
        "model_tpm": q.model_tpm / p.tpm,
        "gateway_toolcalls": q.gateway_toolcall_tps / p.gateway_tps,
        "gateway_connections": q.gateway_connections,
    }
    if q.memory_events_per_task_min > 0:
        limits["memory_events"] = q.memory_create_event_tps / (q.memory_events_per_task_min / 60)
    binding = min(limits, key=limits.get)
    c = int(headroom * limits[binding])
    ramp_seconds = c / q.session_create_tps
    return {"C": c, "binding_constraint": binding, "limits": {k: round(v, 1) for k, v in limits.items()},
            "ramp_seconds_to_C": round(ramp_seconds, 1), "profile": asdict(p), "quotas": asdict(q)}


def quotas_for_target(c_target: int, p: TaskProfile, headroom: float = 0.8, shards: int = 1) -> dict:
    """Inverse: which quota values are needed per shard to run c_target tasks concurrently."""
    per_shard = c_target / shards / headroom
    return {
        "shards": shards,
        "per_shard_C": int(c_target / shards),
        "runtime_active_sessions": int(per_shard),
        "websearch_tps": round(per_shard * p.search_tps, 1),
        "model_tpm": int(per_shard * p.tpm),
        "gateway_toolcall_tps": round(per_shard * p.gateway_tps, 1),
        "gateway_connections": int(per_shard),
        "session_create_tps_for_5min_ramp": round(per_shard / 300, 1),
    }
