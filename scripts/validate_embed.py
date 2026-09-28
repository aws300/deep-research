#!/usr/bin/env python
"""Validate the semantic-merge strategy before wiring it into the service.

Strategy v2 (what dedupe.py implements):
  1. canonicalize(query) -> strict JSON {topic, entities[], aspects[], time_from, time_to}  (Haiku, temperature 0, today's date injected)
  2. HARD GATE: time range must be identical (None==None or same dates)
  3. embed "topic | entities" with cohere.embed-v4 (input_type search_query) -> cosine
  4. cosine >= HIGH  -> merge ; cosine < LOW -> distinct ; in between -> LLM judge ("same research request?")
The script reports, for curated positive/negative pairs, whether the pipeline produces the right decision and where
the thresholds should sit. Run it whenever you change the canonicalization prompt or the model.

  python scripts/validate_embed.py                     # default suite (prints decisions + suggested HIGH/LOW)
  python scripts/validate_embed.py --pair "调研最近1天的科技新闻" "给出昨天的科技新闻"
  python scripts/validate_embed.py --high 0.92 --low 0.78 --dims 1024
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.dedupe_core import (  # noqa: E402
    CanonForm, canonicalize, cosine, embed_texts, judge_same, merge_decision,
)

POSITIVE_PAIRS = [
    ("调研最近1天的科技新闻", "给出昨天的科技新闻"),
    ("调研最近1天的科技新闻", "Summarize yesterday's tech news"),
    ("2026 年 9 月 AgentCore Runtime V2 相比 V1 有哪些变化", "AgentCore Runtime V2 vs V1: what changed in Sept 2026?"),
    ("AgentCore Runtime V2 和 V1 的区别是什么", "对比一下 AgentCore Runtime 新旧两个版本"),
    ("最近一周量子计算领域的重要进展", "过去 7 天量子计算有什么大新闻"),
    ("Amazon Bedrock AgentCore Web Search 连接器的定价和区域", "AgentCore 自带的 Web Search 工具多少钱、哪些区域能用"),
    ("What changed in AgentCore Runtime V2 versus V1?", "AgentCore Runtime V2 相比 V1 的变化"),
]
NEGATIVE_PAIRS = [
    ("调研最近1天的科技新闻", "调研最近1天的体育新闻"),
    ("调研最近1天的科技新闻", "调研最近一个月的科技新闻"),
    ("AgentCore Runtime V2 相比 V1 的变化", "AgentCore Gateway 的限流功能怎么配置"),
    ("最近一周量子计算领域的重要进展", "最近一周核聚变领域的重要进展"),
    ("Amazon Bedrock AgentCore Web Search 连接器的定价", "Amazon Bedrock Knowledge Bases 的定价"),
    ("给出昨天的科技新闻", "给出昨天的财经新闻"),
    ("AgentCore Runtime V2 相比 V1 的变化", "AgentCore harness 相比 Runtime 的区别"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dims", type=int, default=1024, choices=[256, 512, 1024, 1536])
    ap.add_argument("--high", type=float, default=0.90)
    ap.add_argument("--low", type=float, default=0.60)
    ap.add_argument("--pair", nargs=2, metavar=("A", "B"))
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--region", default=None, help="Bedrock region (default: settings region / AWS_REGION)")
    a = ap.parse_args()
    from deepresearch.config import load_settings
    rt = boto3.client("bedrock-runtime", region_name=a.region or load_settings().region)

    pairs = [("pos", *a.pair)] if a.pair else [("pos", *p) for p in POSITIVE_PAIRS] + [("neg", *p) for p in NEGATIVE_PAIRS]
    texts = sorted({t for _, x, y in pairs for t in (x, y)})
    t0 = time.time(); forms: dict[str, CanonForm] = {t: canonicalize(rt, t, a.today) for t in texts}; canon_ms = (time.time() - t0) * 1000 / len(texts)
    t0 = time.time(); vecs = dict(zip(texts, embed_texts(rt, [forms[t].embed_text() for t in texts], a.dims))); embed_ms = (time.time() - t0) * 1000 / len(texts)

    print(f"canonicalize {canon_ms:.0f} ms/query, embed {embed_ms:.0f} ms/query, dims={a.dims}\n")
    print("canonical forms:")
    for t in texts:
        print(f"  {t!r}\n      -> {forms[t].as_dict()}")
    print()
    wrong = 0; judges = 0; pos_scores = []; neg_scores = []
    for kind, x, y in pairs:
        fx, fy = forms[x], forms[y]
        sim = cosine(vecs[x], vecs[y])
        (pos_scores if kind == "pos" else neg_scores).append(sim if fx.same_time(fy) else None)
        decision, why = merge_decision(fx, fy, sim, a.high, a.low, lambda: judge_same(rt, x, y, fx, fy))
        if "judge" in why:
            judges += 1
        ok = (decision == (kind == "pos"))
        wrong += 0 if ok else 1
        print(f"{'OK ' if ok else 'BAD'} {kind} merge={decision!s:5} sim={sim:.4f} time_eq={fx.same_time(fy)!s:5} why={why:<14} {x!r} ~ {y!r}")
    tp = [s for s in pos_scores if s is not None]; tn = [s for s in neg_scores if s is not None]
    print(f"\ntime-gated positives: min sim={min(tp):.4f}   time-gated negatives: max sim={max(tn) if tn else float('nan'):.4f}   "
          f"time gate alone removed {sum(1 for s in neg_scores if s is None)}/{len(neg_scores)} negatives")
    print(f"errors={wrong}/{len(pairs)}  llm_judge_used={judges}  HIGH={a.high} LOW={a.low}")
    if tp and tn:
        print(f"suggestion: LOW ≈ {max(tn) - 0.02:.2f} (below max time-gated negative), HIGH ≈ {min(tp) + 0.02:.2f} if you want to avoid the judge for typical positives")
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
