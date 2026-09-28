#!/usr/bin/env python
"""Operate the semantic-merge cache (Valkey).

  python scripts/dedupe_cache.py stats
  python scripts/dedupe_cache.py lookup "调研最近1天的科技新闻"        # canonical form + whether an equivalent task exists
  python scripts/dedupe_cache.py clear                                 # wipe all merge entries (tasks themselves are untouched)
  python scripts/dedupe_cache.py resolve "问题" --depth quick          # what submit would do (creates/merges a real task!)
Mode: DR_DEDUPE_MODE=direct|lambda|auto (default auto: direct Valkey when reachable, else the VPC Lambda).
TTL:  DR_DEDUPE_TTL_SECONDS overrides dedupe.ttl_seconds for new entries.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.config import load_settings  # noqa: E402
from deepresearch.dedupe import DedupeClient  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("action", choices=["stats", "lookup", "clear", "resolve"])
ap.add_argument("query", nargs="?")
ap.add_argument("--depth", default="quick")
a = ap.parse_args()
c = DedupeClient(load_settings())
if a.action in ("lookup", "resolve") and not a.query:
    ap.error("query required")
if a.action == "stats":
    out = c.call("stats")
elif a.action == "clear":
    out = c.call("clear")
elif a.action == "lookup":
    out = c.call("lookup", query=a.query); out.pop("vec", None)
else:
    out = c.resolve(query=a.query, depth=a.depth, actor_id="cli", source="cli")
print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
