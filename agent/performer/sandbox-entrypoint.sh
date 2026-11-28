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
#   SANDBOX_PROXY_WAIT_SECONDS    how long to wait for the egress proxy (default 60)
#   SANDBOX_PYTHON                the interpreter that checks the proxy answers (default python3)
#   SANDBOX_PROXY_HEARTBEAT_SECONDS  how often the proxy is kept alive while the job runs (default 30)
#   PERFORMER_EGRESS_PROXY        447: the egress proxy, this container's only way out (see below)
#   PERFORMER_EGRESS_ALLOWLIST    refused here: see below
#
# 450: the container's environment holds the stage job's GitHub token and the operator's
# env_passthrough values (a model key), and QAre runs the pull request's code as root in
# here, where it reads every process's initial environment (/proc/<pid>/environ). A process
# keeps that environment for as long as it runs, so only the job, exec'd last, takes the
# container's; it lets go of the token itself before QAre starts (performer.stage_jobs). What
# this script leaves running beside the job (dockerd, the containerd it starts, and the
# proxy heartbeat) starts from an empty environment with only the names it needs.

base="${PERFORMER_BASE_ENTRYPOINT:-/entrypoint.sh}"
wait_seconds="${SANDBOX_DOCKERD_WAIT_SECONDS:-120}"
log="${SANDBOX_DOCKERD_LOG:-/var/log/dockerd.log}"
mountinfo="${SANDBOX_MOUNTINFO:-/proc/self/mountinfo}"

# 450: run "$@" with PATH and the proxy variables that are set, and nothing else of this
# environment. dockerd needs PATH for containerd and runc, and the proxy for image pulls.
clean_env() {
  env -i PATH="${PATH}" \
    ${HTTP_PROXY:+"HTTP_PROXY=${HTTP_PROXY}"} ${HTTPS_PROXY:+"HTTPS_PROXY=${HTTPS_PROXY}"} \
    ${NO_PROXY:+"NO_PROXY=${NO_PROXY}"} ${http_proxy:+"http_proxy=${http_proxy}"} \
    ${https_proxy:+"https_proxy=${https_proxy}"} ${no_proxy:+"no_proxy=${no_proxy}"} \
    "$@"
}

under_sysbox=0
# Sysbox serves /proc and /sys files through FUSE: "... - fuse sysboxfs ...".
if grep -Eq ' - fuse(\.[^ ]*)? sysboxfs ' "${mountinfo}" 2>/dev/null; then
  under_sysbox=1
fi

# The base entry point's egress allowlist is iptables OUTPUT rules in this container.
# Containers the inner engine starts send through FORWARD, past them, and root in here could
# rewrite them anyway. coordinare never starts a sandbox with it (it sets
# PERFORMER_EGRESS_PROXY instead, below); refuse if anything does.
if [ -n "${PERFORMER_EGRESS_ALLOWLIST:-}" ]; then
  echo "ERROR: PERFORMER_EGRESS_ALLOWLIST is set, and it would not cover the containers this sandbox's Docker starts; refusing to start" >&2
  exit 1
fi

# 447: with an egress allowlist, coordinare runs this container on a Docker network of its
# own that has no route out, and PERFORMER_EGRESS_PROXY names the allowlisting proxy, in
# another container, that is its only exit. Enforcement is out there, where nothing in this
# container can change it; this only waits for the proxy, before dockerd or the job start,
# and points this container's own traffic (the job, QAre, git, gh, model calls, and dockerd's
# image pulls) at it. The app QAre boots stays direct on localhost. A proxy that does not
# answer stops the container, so the run is retried rather than run without its way out.
proxy="${PERFORMER_EGRESS_PROXY:-}"
if [ -n "${proxy}" ]; then
  hostport="${proxy#*://}"
  hostport="${hostport%%/*}"
  proxy_wait="${SANDBOX_PROXY_WAIT_SECONDS:-60}"
  waited=0
  until "${SANDBOX_PYTHON:-python3}" -c 'import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 2).close()' \
      "${hostport%:*}" "${hostport##*:}" >/dev/null 2>&1; do
    if [ "${waited}" -ge "${proxy_wait}" ]; then
      echo "ERROR: the egress proxy ${proxy} did not answer within ${proxy_wait}s; refusing to start without it" >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  # The proxy exits once nothing has connected for a few minutes (coordinare starts it with
  # --idle-exit): connect every half minute for as long as the job (this PID, after the exec
  # below) runs, so it goes soon after the job does. Stopping this only cuts the job off.
  # 450: a shell of its own (a subshell would keep this one's environment), given only PATH.
  heartbeat="${SANDBOX_PROXY_HEARTBEAT_SECONDS:-30}"
  env -i PATH="${PATH}" sh -c '
    while kill -0 "$1" 2>/dev/null; do
      sleep "$2"
      "$3" -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 2).close()" \
        "$4" "$5" >/dev/null 2>&1
    done' heartbeat "$$" "${heartbeat}" "${SANDBOX_PYTHON:-python3}" "${hostport%:*}" "${hostport##*:}" \
    </dev/null >/dev/null 2>&1 &
  export HTTP_PROXY="${proxy}" HTTPS_PROXY="${proxy}" http_proxy="${proxy}" https_proxy="${proxy}"
  export NO_PROXY="localhost,127.0.0.1,::1" no_proxy="localhost,127.0.0.1,::1"
  echo "INFO: egress goes out through ${proxy} only" >&2
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
  # Its own unix socket only, never a TCP listener. 450: never with this environment (env
  # execs dockerd, so $! is dockerd's pid).
  clean_env dockerd >>"${log}" 2>&1 &
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
