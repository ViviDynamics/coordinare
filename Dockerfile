FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY config.example.yaml /app/config.example.yaml
COPY .env.example /app/.env.example

ENV COORDINARE_RUN_MODE=compose

CMD ["python", "-m", "coordinare", "--config", "/app/config.yaml"]
