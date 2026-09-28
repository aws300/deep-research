#!/usr/bin/env python
"""Build the region/account-agnostic deployment artifacts and (optionally) push them to Docker Hub.

  artifacts image  docker.io/<repo>:deepresearch-artifacts-<version>   FROM scratch, one layer:
                     /lambda.zip            dedupe, MCP tools, CFN helper Lambdas (src/, settings.yaml, pyyaml, redis, boto3; arm64)
                     /live_runtime.zip      AgentCore Runtime code deploy (live_main.py, src/, config/settings.yaml, mcp; arm64)
                     /tools_schema.json     Gateway Lambda target tool schema (single source: infra/mcp_tools.TOOL_SCHEMA)
                     /skills/deep-research-harness/**   harness skill (uploaded to S3, referenced by the harness)
                     /manifest.json         version + sha256 of every file
  app image        docker.io/<repo>:deepresearch-app-<version>          dispatcher/worker (linux/arm64, ECS Fargate / EKS)
                   dependencies are vendored into build/app-deps first, so the Dockerfile has no RUN step

The CloudFormation template (deploy/cloudformation/deepresearch.yaml) copies the artifacts layer into the stack's bucket
with a custom resource, so a deployment needs no local build, no ECR and no account-specific image.

  python deploy/build_artifacts.py                 # build into build/artifacts only
  python deploy/build_artifacts.py --push          # + docker build/push both images (docker login first)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "artifacts"
PIP_TARGET = ["--retries", "10", "--timeout", "60", "--platform", "manylinux2014_aarch64", "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:"]
LAMBDA_DEPS = ["pyyaml>=6", "redis>=5,<6", "boto3>=1.42"]   # vendored boto3: harness APIs may be newer than the runtime's
RUNTIME_DEPS = ["mcp>=1.20,<2", "boto3>=1.42", "pyyaml>=6", "redis>=5,<6"]
APP_DEPS = ["boto3>=1.42", "pyyaml>=6", "redis>=5,<6"]
APP_DEPS_DIR = ROOT / "build" / "app-deps"      # COPY-ed by the repo-root Dockerfile (no RUN step needed)
SKIP = ("__pycache__", ".pyc")


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _vendor(deps: list[str], dest: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(dest), *PIP_TARGET, *deps], check=True)


def _add_tree(z: zipfile.ZipFile, base: Path, arc_prefix: str = "") -> None:
    for p in sorted(base.rglob("*")):
        if p.is_file() and not any(s in p.as_posix() for s in SKIP):
            z.write(p, arc_prefix + p.relative_to(base).as_posix())


def _add_code(z: zipfile.ZipFile) -> None:
    for p in sorted((ROOT / "src").rglob("*.py")):
        if "__pycache__" not in p.parts:
            z.write(p, p.relative_to(ROOT).as_posix())
    z.write(ROOT / "config" / "settings.yaml", "config/settings.yaml")   # generic defaults only (never local.yaml)


def build_lambda_zip(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        _vendor(LAMBDA_DEPS, Path(td))
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            _add_tree(z, Path(td))
            _add_code(z)
            z.writestr("dedupe_function.py", "from deepresearch.dedupe_lambda import lambda_handler  # noqa: F401\n")
            z.writestr("tools_function.py", "from deepresearch.mcp_tools_lambda import lambda_handler  # noqa: F401\n")
            z.writestr("cfn_function.py", "from deepresearch.cfn_custom import lambda_handler  # noqa: F401\n")


def build_runtime_zip(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        _vendor(RUNTIME_DEPS, Path(td))
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            _add_tree(z, Path(td))
            _add_code(z)
            z.write(ROOT / "deploy" / "runtime" / "live_main.py", "live_main.py")


def build(out: Path = OUT) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from deepresearch.infra.mcp_tools import TOOL_SCHEMA

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    build_lambda_zip(out / "lambda.zip")
    build_runtime_zip(out / "live_runtime.zip")
    if APP_DEPS_DIR.exists():
        shutil.rmtree(APP_DEPS_DIR)
    _vendor(APP_DEPS, APP_DEPS_DIR)
    (out / "tools_schema.json").write_text(json.dumps(TOOL_SCHEMA, ensure_ascii=False, indent=1))
    shutil.copytree(ROOT / "skills" / "deep-research-harness", out / "skills" / "deep-research-harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"version": version(), "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def _docker(*args: str) -> None:
    print("+ docker", " ".join(args), flush=True)
    subprocess.run(["docker", *args], check=True)


def push(repo: str, ver: str, latest: bool) -> list[str]:
    art = f"{repo}:deepresearch-artifacts-{ver}"
    app = f"{repo}:deepresearch-app-{ver}"
    # --provenance=false: a plain single-platform manifest (the CloudFormation copier reads layers[0] directly)
    common = ["buildx", "build", "--platform", "linux/arm64", "--provenance=false", "--sbom=false", "--push"]
    _docker(*common, "-f", str(ROOT / "deploy" / "artifacts.Dockerfile"), "-t", art,
            *(["-t", f"{repo}:deepresearch-artifacts-latest"] if latest else []), str(ROOT / "build"))
    _docker(*common, "-f", str(ROOT / "Dockerfile"), "-t", app,
            *(["-t", f"{repo}:deepresearch-app-latest"] if latest else []), str(ROOT))
    return [art, app]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="build and push both images")
    ap.add_argument("--repo", default="aws300/deploy", help="Docker Hub repository (default aws300/deploy)")
    ap.add_argument("--version", default=None, help="image version (default: pyproject.toml version)")
    ap.add_argument("--latest", action="store_true", help="also tag *-latest")
    a = ap.parse_args()
    ver = a.version or version()
    m = build()
    m["version"] = ver
    (OUT / "manifest.json").write_text(json.dumps(m, indent=1))
    for f in ("lambda.zip", "live_runtime.zip"):
        print(f"{f:18s} {(OUT / f).stat().st_size / 1e6:6.1f} MB")
    print(f"built {len(m['files'])} files into {OUT}")
    if a.push:
        for ref in push(a.repo, ver, a.latest):
            print("pushed docker.io/" + ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
