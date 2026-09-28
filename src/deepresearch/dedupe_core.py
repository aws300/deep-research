"""Semantic-merge primitives (no cache dependency): canonicalization, embeddings, decision rule.

Used by scripts/validate_embed.py and by dedupe.py (the Valkey-backed service).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

EMBED_MODEL = "us.cohere.embed-v4:0"          # on-demand needs an inference profile
CANON_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

CANON_SYSTEM = """You convert a research request into a canonical JSON descriptor so that requests with the same meaning
produce the same descriptor. Today is {today}. Output ONLY a JSON object with exactly these keys:
- "topic": the BARE subject as a short English noun phrase (lowercase). No dates, no verbs, and NO facet words such as
  news/developments/changes/pricing/comparison (those go to "aspects"). Product and version names verbatim,
  e.g. "agentcore runtime v2 vs v1", "quantum computing", "technology news" -> topic "technology", aspects ["news"].
- "entities": list of lowercase English product/organisation/technology names mentioned or clearly implied (sorted)
- "aspects": list of lowercase English facets requested (e.g. "pricing", "regions", "changes", "news"), sorted; use [] if generic
- "time_from", "time_to": absolute dates YYYY-MM-DD when the request has a time scope, else null.
  yesterday / 最近1天 / 过去24小时 / last day -> both = yesterday. last week / 最近一周 / 过去7天 -> 7 days ending yesterday.
  last month / 最近一个月 -> 30 days ending yesterday. this month -> month to date. Explicit months keep their range.
  A month/year that merely qualifies a product release or version (e.g. "V2 released in Sept 2026", "2026 年 9 月的 Runtime V2")
  is NOT a time scope -> null. Version comparisons, pricing, how-to questions -> null.
Do not translate product names, do not add commentary, no markdown fences."""

JUDGE_SYSTEM = """You decide whether two research requests ask for the SAME deep-research report (same subject, same facets,
same time scope) so that one report could answer both. Different subject domain (e.g. tech news vs sports news), different
product, or different time window means NOT the same. Answer with exactly one word: SAME or DIFFERENT."""


@dataclass
class CanonForm:
    topic: str
    entities: list[str] = field(default_factory=list)
    aspects: list[str] = field(default_factory=list)
    time_from: str | None = None
    time_to: str | None = None
    raw: str = ""

    def embed_text(self) -> str:
        ents = " ".join(sorted(set(self.entities)))
        asp = " ".join(sorted(set(self.aspects)))
        return f"{self.topic} | {ents} | {asp}".strip()

    def same_time(self, other: "CanonForm") -> bool:
        return (self.time_from, self.time_to) == (other.time_from, other.time_to)

    def exact_key(self) -> str:
        return json.dumps({"t": self.topic, "e": sorted(set(self.entities)), "a": sorted(set(self.aspects)),
                           "f": self.time_from, "u": self.time_to}, sort_keys=True, ensure_ascii=False)

    def as_dict(self) -> dict:
        d = asdict(self); d.pop("raw", None); return d


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def canonicalize(rt, text: str, today: str) -> CanonForm:
    r = rt.converse(modelId=CANON_MODEL, system=[{"text": CANON_SYSTEM.format(today=today)}],
                    messages=[{"role": "user", "content": [{"text": text}]}],
                    inferenceConfig={"maxTokens": 300, "temperature": 0})
    out = r["output"]["message"]["content"][0]["text"].strip()
    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        d = {"topic": _norm(text)}
    return CanonForm(topic=_norm(str(d.get("topic") or text)),
                     entities=sorted({_norm(str(e)) for e in (d.get("entities") or []) if str(e).strip()}),
                     aspects=sorted({_norm(str(x)) for x in (d.get("aspects") or []) if str(x).strip()}),
                     time_from=d.get("time_from") or None, time_to=d.get("time_to") or None, raw=out)


def embed_texts(rt, texts: list[str], dims: int = 1024) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 48):
        body = {"texts": texts[i:i + 48], "input_type": "search_query", "embedding_types": ["float"],
                "output_dimension": dims, "truncate": "RIGHT"}
        r = rt.invoke_model(modelId=EMBED_MODEL, body=json.dumps(body), contentType="application/json", accept="application/json")
        emb = json.loads(r["body"].read())["embeddings"]
        out.extend(emb["float"] if isinstance(emb, dict) else emb)
    return out


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def judge_same(rt, a: str, b: str, fa: CanonForm | None = None, fb: CanonForm | None = None) -> bool:
    msg = f"Request A: {a}\nRequest B: {b}"
    if fa and fb:
        msg += f"\nDescriptor A: {json.dumps(fa.as_dict(), ensure_ascii=False)}\nDescriptor B: {json.dumps(fb.as_dict(), ensure_ascii=False)}"
    r = rt.converse(modelId=CANON_MODEL, system=[{"text": JUDGE_SYSTEM}], messages=[{"role": "user", "content": [{"text": msg}]}],
                    inferenceConfig={"maxTokens": 5, "temperature": 0})
    return r["output"]["message"]["content"][0]["text"].strip().upper().startswith("SAME")


def merge_decision(fa: CanonForm, fb: CanonForm, sim: float, high: float, low: float,
                   judge: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """Return (merge?, reason). Time scope is a hard gate; exact canonical match short-circuits; grey zone -> judge."""
    a_t, b_t = (fa.time_from, fa.time_to), (fb.time_from, fb.time_to)
    one_sided_time = (a_t == (None, None)) != (b_t == (None, None))
    if a_t != b_t and not one_sided_time:
        return False, "time_gate"          # both have a scope and they differ -> different reports
    if fa.exact_key() == fb.exact_key():
        return True, "exact_canon"
    if one_sided_time:                     # only one side carries a scope: let the judge decide (never auto-merge)
        if judge is None or sim < low:
            return False, "time_onesided"
        return judge(), "judge_time"
    if sim >= high:
        return True, "sim_high"
    if sim < low:
        return False, "sim_low"
    if judge is None:
        return False, "grey_nojudge"
    return (judge(), "judge") if True else (False, "judge")
