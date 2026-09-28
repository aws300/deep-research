# Streaming MCP server for AgentCore Runtime (linux/arm64, port 8000, /mcp)
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.20,<2" "boto3>=1.42" "pyyaml>=6" "aws-opentelemetry-distro>=0.18.0"
COPY src ./src
COPY config/settings.yaml ./config/settings.yaml
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["opentelemetry-instrument", "python", "-m", "deepresearch.live_mcp_server"]
