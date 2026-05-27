#!/usr/bin/env bash
# Smoke test (SC-006 gate for spec 073): drive the claude CLI through the
# in-process LiteLLM response-translation shim and assert zero "Content block
# not found" parser errors across 5 sequential calls.
#
# The shim is the same one the performer launches (claude_code_shim.py): it
# binds 127.0.0.1:<ephemeral>, forwards POST /v1/messages to the upstream
# LiteLLM proxy, and strips `thinking` content blocks the CLI parser rejects.
#
# Usage: scripts/smoke_claude_via_litellm.sh [model]
#   model defaults to spark/qwen3.6:35b (matches config.claude.yaml)
#
# Requires LITELLM_MASTER_KEY in .env at repo root.

set -u
set -o pipefail

MODEL="${1:-spark/qwen3.6:35b}"
UPSTREAM_URL="https://litellm.vividynamics.com"
N_CALLS=5

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found at repo root" >&2
  exit 2
fi

MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env | head -1 | cut -d= -f2-)"
if [[ -z "${MASTER_KEY}" ]]; then
  echo "ERROR: LITELLM_MASTER_KEY not set in .env" >&2
  exit 2
fi

REDACTED="${MASTER_KEY:0:6}…${MASTER_KEY: -4}"
echo "model              : ${MODEL}"
echo "upstream (shim->)  : ${UPSTREAM_URL}"
echo "ANTHROPIC_AUTH_TOKEN: ${REDACTED}"
echo "calls              : ${N_CALLS}"
echo

# Workdir for shim port file + captured stderr.
WORKDIR="$(mktemp -d -t claude-litellm-smoke.XXXXXX)"
PORTFILE="${WORKDIR}/port"
SHIM_LOG="${WORKDIR}/shim.log"
STDERR_LOG="${WORKDIR}/claude.stderr"
: > "${STDERR_LOG}"

cleanup() {
  if [[ -n "${SHIM_PID:-}" ]] && kill -0 "${SHIM_PID}" 2>/dev/null; then
    kill "${SHIM_PID}" 2>/dev/null || true
    wait "${SHIM_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Spawn the shim in a Python subprocess. Writes the bound port to PORTFILE
# once listening, then runs until killed.
PYBIN="python3"
if [[ -x agent/performer/.venv/bin/python ]]; then
  PYBIN="agent/performer/.venv/bin/python"
fi

PYTHONPATH="agent/performer/src" \
UPSTREAM_URL="${UPSTREAM_URL}" \
UPSTREAM_TOKEN="${MASTER_KEY}" \
PORTFILE="${PORTFILE}" \
"${PYBIN}" -u -c '
import asyncio, os, sys
from performer.backends.claude_code_shim import ClaudeCodeShim

async def main():
    shim = ClaudeCodeShim(os.environ["UPSTREAM_URL"], os.environ["UPSTREAM_TOKEN"])
    base = await shim.start()
    port = base.rsplit(":", 1)[1]
    with open(os.environ["PORTFILE"], "w") as f:
        f.write(port)
    try:
        await asyncio.Event().wait()
    finally:
        await shim.stop()

asyncio.run(main())
' >"${SHIM_LOG}" 2>&1 &
SHIM_PID=$!

# Wait up to 10s for the shim to bind.
for _ in $(seq 1 50); do
  if [[ -s "${PORTFILE}" ]]; then break; fi
  if ! kill -0 "${SHIM_PID}" 2>/dev/null; then
    echo "ERROR: shim exited before binding. Log:" >&2
    cat "${SHIM_LOG}" >&2
    exit 2
  fi
  sleep 0.2
done
if [[ ! -s "${PORTFILE}" ]]; then
  echo "ERROR: shim did not write port within 10s" >&2
  cat "${SHIM_LOG}" >&2
  exit 2
fi
PORT="$(cat "${PORTFILE}")"
echo "shim listening     : 127.0.0.1:${PORT}"
echo

unset ANTHROPIC_API_KEY
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}"
export ANTHROPIC_AUTH_TOKEN="${MASTER_KEY}"

RC_TOTAL=0
for i in $(seq 1 "${N_CALLS}"); do
  echo "--- call ${i}/${N_CALLS} ---"
  gtimeout 60 claude --print --model "${MODEL}" "say hi in 3 words (attempt ${i})" 2>>"${STDERR_LOG}"
  RC=$?
  echo "--- call ${i} exit: ${RC} ---"
  if [[ "${RC}" -ne 0 ]]; then
    RC_TOTAL=$((RC_TOTAL + 1))
  fi
done

echo
if grep -q "Content block not found" "${STDERR_LOG}"; then
  echo "FAIL: 'Content block not found' detected in stderr — shim did not strip thinking blocks." >&2
  echo "--- stderr ---" >&2
  cat "${STDERR_LOG}" >&2
  exit 1
fi

if [[ "${RC_TOTAL}" -ne 0 ]]; then
  echo "FAIL: ${RC_TOTAL}/${N_CALLS} CLI invocations exited non-zero (no parser hits, but CLI errored)." >&2
  echo "--- stderr ---" >&2
  cat "${STDERR_LOG}" >&2
  exit 1
fi

echo "PASS: ${N_CALLS}/${N_CALLS} calls succeeded; zero 'Content block not found' in stderr."
