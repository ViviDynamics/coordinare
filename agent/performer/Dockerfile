FROM python:3.12-slim

# Install git (clone/push) and curl (opencode install)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install opencode CLI
RUN curl -fsSL https://opencode.ai/install.sh | sh

# Install performer package
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "performer"]
