import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("DR_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="set DR_E2E=1 (and deploy first) to run end-to-end tests")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
