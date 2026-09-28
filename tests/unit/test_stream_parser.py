import json

from deepresearch.harness_client import parse_stream, summarize_tool_input
from deepresearch.worker import _parse_status


def _raw():
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "Plan: 3 queries"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockStart": {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "t1", "name": "research_tools___web-search___WebSearch"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"query": "Agent'}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": 'Core Runtime V2"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"contentBlockStart": {"contentBlockIndex": 2, "start": {"toolResult": {"toolUseId": "t1"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {"toolResult": [{"text": "{\"results\": []}"}]}}},
        {"contentBlockStop": {"contentBlockIndex": 2}},
        {"contentBlockDelta": {"contentBlockIndex": 3, "delta": {"text": "Done. "}}},
        {"contentBlockDelta": {"contentBlockIndex": 3, "delta": {"text": 'DR_STATUS {"report_path": "/tmp/research/t1/report.md", "sources": 12, "searches": 18, "validated": true}'}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 400}, "metrics": {"latencyMs": 900}}},
    ]


def test_parse_stream_normalises_events():
    evs = list(parse_stream(_raw()))
    kinds = [e["type"] for e in evs]
    assert kinds == ["reasoning", "tool_use", "tool_result", "text", "text", "stop", "usage"]
    tu = evs[1]
    assert tu["name"].endswith("WebSearch") and json.loads(tu["input"])["query"] == "AgentCore Runtime V2"
    assert evs[-2]["reason"] == "end_turn"
    assert evs[-1]["cache_read"] == 400


def test_status_line_extraction():
    text = "".join(e["text"] for e in parse_stream(_raw()) if e["type"] == "text")
    st = _parse_status(text)
    assert st["sources"] == 12 and st["validated"] is True and st["report_path"].endswith("report.md")


def test_summarize_tool_input():
    assert summarize_tool_input('{"query": "x y z", "maxResults": 8}') == "x y z"
    assert summarize_tool_input("not json") == "not json"


def test_error_event_surfaces():
    evs = list(parse_stream([{"validationException": {"message": "bad", "reason": "FieldValidationFailed"}}]))
    assert evs == [{"type": "error", "kind": "validationException", "message": "bad"}]
