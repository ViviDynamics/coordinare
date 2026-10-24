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
COPY config.example*.yaml config.minimal.yaml ./

# pip and its vendored certifi from verified wheels first: the base image's pip
# still trusts the distrusted emSign CAs (#361).
RUN python -m pip install --no-cache-dir --no-deps \
    "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    "certifi @ https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl#sha256=42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b"

RUN pip install --no-cache-dir ./packages/ci_detection ./packages/service_inference ./packages/routing_models .

COPY config.example.yaml /app/config.example.yaml
COPY .env.example /app/.env.example

ENV COORDINARE_RUN_MODE=compose

CMD ["python", "-m", "coordinare", "--config", "/app/config.yaml"]
