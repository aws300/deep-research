# Dispatcher / worker image (linux/arm64). Region, account and resource ids come from DR_* environment variables.
# Build via `python deploy/build_artifacts.py --push` (vendors arm64 dependencies into build/app-deps first).
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY build/app-deps /opt/deps
COPY src ./src
COPY config/settings.yaml ./config/settings.yaml
COPY skills ./skills
COPY scripts/run_dispatcher.py scripts/debug_task.py ./scripts/
ENV PYTHONPATH=/app/src:/opt/deps PYTHONUNBUFFERED=1 DR_NO_LOCAL=1
USER 10001
CMD ["python", "scripts/run_dispatcher.py"]
