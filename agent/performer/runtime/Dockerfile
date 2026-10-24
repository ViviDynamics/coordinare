# Optional platform image. BASE_IMAGE must be explicitly pinned by the operator.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# The platform guest matches this carrier's CPU, so it runs without emulation. The other CPU is reached through a static
# user-mode interpreter (qemu-user-static) that the guest keeps, not through a second full-system guest.
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) guest='qemu-system-x86' ;; \
      aarch64) guest='qemu-system-arm qemu-efi-aarch64' ;; \
      *) echo 'unsupported carrier architecture' >&2; exit 1 ;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends \
    openssh-client qemu-utils genisoimage qemu-user-static $guest \
    docker-cli docker-compose docker-buildx \
    && rm -rf /var/lib/apt/lists/*
COPY agent/performer/runtime/prepare_guest.py /opt/coordinare-runtime/prepare_guest.py
# Pinned Alpine 3.23.6 cloud images, one per CPU; SHA-512 values are the official .sha512 files.
ARG PLATFORM_GUEST_URL_X86_64=https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/alpine-3.23.6-x86_64-bios-cloudinit-r1.qcow2
ARG PLATFORM_GUEST_SHA512_X86_64=2853e42ba2696ee0e23bcdae6a640b8f84f9ce51de4cf4cb4728dc6a03760a0c520f181a8ab018592b4f5f9bc9d7d3c7e6fc5bcd10beabee7be7a26b5cd5b76c
ARG PLATFORM_GUEST_URL_AARCH64=https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/alpine-3.23.6-aarch64-uefi-cloudinit-r1.qcow2
ARG PLATFORM_GUEST_SHA512_AARCH64=d13783e02020662cdd8e88359654881a178948a851207baa94dd57b3c69dd1fd6f1a3dacec2165ec557ff2eb7451caaf0f15f5efca561abe24473b26169c4c41
# Preparation sees no consumer source and pulls no project/container images.
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) url="$PLATFORM_GUEST_URL_X86_64"; sha="$PLATFORM_GUEST_SHA512_X86_64" ;; \
      aarch64) url="$PLATFORM_GUEST_URL_AARCH64"; sha="$PLATFORM_GUEST_SHA512_AARCH64" ;; \
      *) echo 'unsupported carrier architecture' >&2; exit 1 ;; \
    esac; \
    setpriv --bounding-set=-all --no-new-privs python /opt/coordinare-runtime/prepare_guest.py \
    --arch "$(uname -m)" --url "$url" --sha512 "$sha"
# Keep platform preparation cached when trusted application code changes.
COPY packages/service_inference/ /opt/coordinare-service-inference/
RUN pip install --no-cache-dir --no-deps /opt/coordinare-service-inference
COPY agent/performer/pyproject.toml agent/performer/README.md /opt/coordinare-performer/
COPY agent/performer/src/ /opt/coordinare-performer/src/
RUN pip install --no-cache-dir --no-deps /opt/coordinare-performer
