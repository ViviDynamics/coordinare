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
# pip and its vendored certifi from verified wheels first: the base image's pip
# still trusts the distrusted emSign CAs (#361).
RUN python -m pip install --no-cache-dir --no-deps \
    "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    "certifi @ https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl#sha256=42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b"

RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "performer"]
