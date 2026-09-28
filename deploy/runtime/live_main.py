"""AgentCore Runtime entry point (direct code deploy, PYTHON_3_12): streaming MCP server on 0.0.0.0:8000/mcp.

The code zip keeps the repository layout (src/deepresearch, config/settings.yaml) with dependencies vendored at the root.
All environment-specific values come from DR_* environment variables set by the CloudFormation template.
"""
import logging
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "src"), HERE]   # package + vendored dependencies, independent of how python is launched
os.environ.setdefault("DR_NO_LOCAL", "1")

logging.basicConfig(level=os.environ.get("DR_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
runpy.run_module("deepresearch.live_mcp_server", run_name="__main__")
