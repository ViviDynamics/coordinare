FROM coordinare-performer:base

# Install build tools and Python dev utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential make \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS via NodeSource with GPG verification (avoids curl|bash)
RUN apt-get update && apt-get install -y --no-install-recommends gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ENTRYPOINT inherited from base image: ["python", "-m", "performer"]
