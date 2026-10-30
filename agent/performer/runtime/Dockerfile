# Optional platform image. BASE_IMAGE must be explicitly pinned by the operator.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client qemu-system-x86 qemu-utils genisoimage \
    docker-cli docker-compose docker-buildx \
    && rm -rf /var/lib/apt/lists/*
COPY agent/performer/runtime/prepare_guest.py /opt/coordinare-runtime/prepare_guest.py
ARG PLATFORM_GUEST_URL=https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/alpine-3.23.6-x86_64-bios-cloudinit-r1.qcow2
ARG PLATFORM_GUEST_SHA512=2853e42ba2696ee0e23bcdae6a640b8f84f9ce51de4cf4cb4728dc6a03760a0c520f181a8ab018592b4f5f9bc9d7d3c7e6fc5bcd10beabee7be7a26b5cd5b76c
# Preparation sees no consumer source and pulls no project/container images.
RUN setpriv --bounding-set=-all --no-new-privs python /opt/coordinare-runtime/prepare_guest.py \
    --url "$PLATFORM_GUEST_URL" --sha512 "$PLATFORM_GUEST_SHA512"
# Keep platform preparation cached when trusted application code changes.
COPY packages/service_inference/ /opt/coordinare-service-inference/
RUN pip install --no-cache-dir --no-deps /opt/coordinare-service-inference
COPY agent/performer/pyproject.toml agent/performer/README.md /opt/coordinare-performer/
COPY agent/performer/src/ /opt/coordinare-performer/src/
RUN pip install --no-cache-dir --no-deps /opt/coordinare-performer
