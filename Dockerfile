FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# pyproject depends on coordinare-service-inference, which resolves through
# [tool.uv.sources] to this path. pip does not read that table and 404s looking
# for the name on PyPI, so the local package is installed first and satisfies
# the requirement by being present. Dockerfile.daemon avoids this by using uv.
COPY packages ./packages
# 193: the wheel force-includes the example configs (pyproject.toml), so they
# must be in place before the install below.
COPY config.example*.yaml ./

RUN pip install --no-cache-dir ./packages/ci_detection ./packages/service_inference ./packages/routing_models .

COPY config.example.yaml /app/config.example.yaml
COPY .env.example /app/.env.example

ENV COORDINARE_RUN_MODE=compose

CMD ["python", "-m", "coordinare", "--config", "/app/config.yaml"]
