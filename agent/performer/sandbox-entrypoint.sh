#!/bin/sh
# 435: entry point of the QAre Docker sandbox image (Dockerfile.sandbox).
#
# A QAre provider with `sandbox: sysbox` runs its stage jobs in this image under the
# Sysbox runtime (`docker run --runtime=sysbox-runc`). Sysbox gives the container its own
# user namespace and a virtualised /proc and /sys, so a Docker engine can run inside it
# without --privileged and without the host's Docker socket. This script starts that
# engine, waits until it answers, then hands over to the performer's own entry point,
# which runs the stage job. QAre then finds Docker on its usual socket, the app it boots
# on localhost, and the checkout at the same paths, all inside the job's one container.
# Removing that container (coordinare does it by the job's name) removes the engine and
# everything it started.
#
# Outside Sysbox, dockerd cannot start. The script says so and runs the job without
# Docker, so the image fails no worse than the full image; coordinare's health check
# for a sandboxed provider asks this engine to answer and reports it when it does not.
#
# Environment (tests override the first three):
#   PERFORMER_BASE_ENTRYPOINT     the performer's entry point (default /entrypoint.sh)
#   SANDBOX_DOCKERD_WAIT_SECONDS  how long to wait for dockerd (default 60)
#   SANDBOX_DOCKERD_LOG           where dockerd logs (default /var/log/dockerd.log)
#   PERFORMER_EGRESS_ALLOWLIST    refused here: see below

base="${PERFORMER_BASE_ENTRYPOINT:-/entrypoint.sh}"
wait_seconds="${SANDBOX_DOCKERD_WAIT_SECONDS:-60}"
log="${SANDBOX_DOCKERD_LOG:-/var/log/dockerd.log}"

# The performer's egress allowlist limits what this container sends (iptables OUTPUT).
# Containers the inner engine starts send through FORWARD, past it, so the allowlist would
# hold only in part. coordinare never starts a sandbox with one; refuse if anything does.
if [ -n "${PERFORMER_EGRESS_ALLOWLIST:-}" ]; then
  echo "ERROR: PERFORMER_EGRESS_ALLOWLIST is set, and it would not cover the containers this sandbox's Docker starts; refusing to start" >&2
  exit 1
fi

if command -v dockerd >/dev/null 2>&1; then
  mkdir -p "$(dirname "${log}")"
  # Its own unix socket only, never a TCP listener.
  dockerd >>"${log}" 2>&1 &
  dockerd_pid=$!
  waited=0
  until docker info >/dev/null 2>&1; do
    if ! kill -0 "${dockerd_pid}" 2>/dev/null; then
      echo "WARNING: the sandbox's dockerd exited before it answered; this container is most likely not running under sysbox-runc. Continuing without Docker. Its last lines:" >&2
      tail -n 20 "${log}" >&2 2>/dev/null
      break
    fi
    if [ "${waited}" -ge "${wait_seconds}" ]; then
      echo "WARNING: the sandbox's dockerd did not answer within ${wait_seconds}s (sysbox-runc runtime?); continuing without it. Log: ${log}" >&2
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
else
  echo "WARNING: no dockerd in this image; continuing without Docker" >&2
fi

exec "${base}" "$@"
