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
# Under Sysbox the sandbox was asked for, so an engine that does not answer stops the
# container: QAre never runs without its Docker, where a profile's `docker compose`
# checks would fail and the run would blame the pull request. The job then ends without
# a word and coordinare retries it. Outside Sysbox, dockerd cannot start; the script says
# so and runs the command without Docker, so the image fails no worse than the full
# image (coordinare's health check for a sandboxed provider reports it).
#
# Environment (tests override all but the last):
#   PERFORMER_BASE_ENTRYPOINT     the performer's entry point (default /entrypoint.sh)
#   SANDBOX_DOCKERD_WAIT_SECONDS  how long to wait for dockerd (default 120)
#   SANDBOX_DOCKERD_LOG           where dockerd logs (default /var/log/dockerd.log)
#   SANDBOX_MOUNTINFO             read to tell Sysbox (sysboxfs mounts) (default /proc/self/mountinfo)
#   PERFORMER_EGRESS_ALLOWLIST    refused here: see below

base="${PERFORMER_BASE_ENTRYPOINT:-/entrypoint.sh}"
wait_seconds="${SANDBOX_DOCKERD_WAIT_SECONDS:-120}"
log="${SANDBOX_DOCKERD_LOG:-/var/log/dockerd.log}"
mountinfo="${SANDBOX_MOUNTINFO:-/proc/self/mountinfo}"

under_sysbox=0
if grep -q " - sysboxfs " "${mountinfo}" 2>/dev/null; then
  under_sysbox=1
fi

# The performer's egress allowlist limits what this container sends (iptables OUTPUT).
# Containers the inner engine starts send through FORWARD, past it, so the allowlist would
# hold only in part. coordinare never starts a sandbox with one; refuse if anything does.
if [ -n "${PERFORMER_EGRESS_ALLOWLIST:-}" ]; then
  echo "ERROR: PERFORMER_EGRESS_ALLOWLIST is set, and it would not cover the containers this sandbox's Docker starts; refusing to start" >&2
  exit 1
fi

# No engine: under Sysbox, refuse; elsewhere, say so and go on without Docker.
no_engine() {
  if [ "${under_sysbox}" = "1" ]; then
    echo "ERROR: $1 under sysbox-runc; refusing to start without the sandbox's Docker" >&2
    exit 1
  fi
  echo "WARNING: $1; this container is not running under sysbox-runc. Continuing without Docker." >&2
}

if command -v dockerd >/dev/null 2>&1; then
  mkdir -p "$(dirname "${log}")"
  # Its own unix socket only, never a TCP listener.
  dockerd >>"${log}" 2>&1 &
  dockerd_pid=$!
  waited=0
  until docker info >/dev/null 2>&1; do
    if ! kill -0 "${dockerd_pid}" 2>/dev/null; then
      tail -n 20 "${log}" >&2 2>/dev/null
      no_engine "the sandbox's dockerd exited before it answered (its last lines are above)"
      break
    fi
    if [ "${waited}" -ge "${wait_seconds}" ]; then
      kill "${dockerd_pid}" 2>/dev/null
      no_engine "the sandbox's dockerd did not answer within ${wait_seconds}s (log: ${log})"
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
else
  no_engine "this image has no dockerd"
fi

exec "${base}" "$@"
