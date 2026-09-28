"""Upload the deep-research-harness skill directory to S3 (the harness fetches it once per session)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..aws_clients import client
from ..config import ROOT, Settings

SKILL_DIR = ROOT / "skills" / "deep-research-harness"


def upload_skill(s: Settings) -> str:
    s3 = client("s3", s.harness_region)
    prefix = s["storage"]["skills_prefix"]
    for path in SKILL_DIR.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            key = prefix + path.relative_to(SKILL_DIR).as_posix()
            body = path.read_bytes()
            s3.put_object(Bucket=s.bucket, Key=key, Body=body,
                          Metadata={"sha256": hashlib.sha256(body).hexdigest()[:16]})
    return f"s3://{s.bucket}/{prefix}"
