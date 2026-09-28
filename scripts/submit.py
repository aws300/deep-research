#!/usr/bin/env python
"""Submit a research task and follow its progress. Example:
  python scripts/submit.py "2026 年 AgentCore Runtime V2 相比 V1 的变化" --depth quick --follow
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.api import DeepResearchAPI  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("query")
ap.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
ap.add_argument("--actor", default="cli-user")
ap.add_argument("--follow", action="store_true")
ap.add_argument("--print-report", action="store_true")
a = ap.parse_args()
api = DeepResearchAPI()
t = api.submit(a.query, depth=a.depth, actor_id=a.actor)
print("submitted", t["task_id"], "queue:", api.queue_depth())
if a.follow:
    for ev in api.follow(t["task_id"]):
        k = ev.get("kind")
        if k == "tool_use":
            print(f"  🔧 {ev.get('tool')}: {ev.get('arg')}")
        elif k == "reasoning":
            print(f"  💭 {ev.get('text','')[:160]}")
        elif k == "final":
            print("FINAL", ev["status"], json.dumps(ev.get("result"), ensure_ascii=False, default=str)[:800])
    if a.print_report:
        print(api.report(t["task_id"]))
