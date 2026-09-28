#!/usr/bin/env python
r"""Long-running dispatcher process (local, container, or EKS pod).

  DR_LOG_LEVEL=DEBUG python scripts/run_dispatcher.py --log-file logs/dispatcher.log
Log lines are prefixed with the task_id in brackets so you can grep one task: grep "\[t1790...\]" logs/dispatcher.log
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deepresearch.config import load_settings  # noqa: E402
from deepresearch.dispatcher import Dispatcher  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--log-file", default=os.environ.get("DR_LOG_FILE", ""), help="also write rotating logs here (10 MB x 5)")
ap.add_argument("--log-level", default=os.environ.get("DR_LOG_LEVEL", "INFO"))
a = ap.parse_args()
handlers: list[logging.Handler] = [logging.StreamHandler()]
if a.log_file:
    Path(a.log_file).parent.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.handlers.RotatingFileHandler(a.log_file, maxBytes=10_000_000, backupCount=5))
logging.basicConfig(level=a.log_level.upper(), handlers=handlers,
                    format="%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s")
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
d = Dispatcher(load_settings())
signal.signal(signal.SIGTERM, lambda *_: d.stop())
signal.signal(signal.SIGINT, lambda *_: d.stop())
d.run_forever()
