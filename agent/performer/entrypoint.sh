#!/bin/sh
# Install (or upgrade) the backend CLI specified by $BACKEND, then exec the
# performer server.  Upgrade failure is non-fatal: we log a warning and
# continue so a transient network error does not knock a container out of
# the pool.  If BACKEND is unset the performer starts without any CLI
# installed; the capability probe will advertise no backends.

case "${BACKEND:-}" in
  codex)
    npm install -g @openai/codex@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: codex upgrade failed, continuing with installed version" >&2
    ;;
  claude)
    npm install -g @anthropic-ai/claude-code@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: claude-code upgrade failed, continuing with installed version" >&2
    ;;
  opencode)
    npm install -g opencode-ai@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: opencode upgrade failed, continuing with installed version" >&2
    ;;
  junie)
    npm install -g @jetbrains/junie-cli@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: junie upgrade failed, continuing with installed version" >&2
    ;;
  cursor)
    curl -fsSL https://cursor.com/install | sh 2>&1 \
      || echo "WARNING: cursor upgrade failed, continuing with installed version" >&2
    ;;
  "")
    echo "INFO: BACKEND not set — starting performer without a backend CLI" >&2
    ;;
  *)
    echo "WARNING: unknown BACKEND '${BACKEND}' — starting performer without a backend CLI" >&2
    ;;
esac

# Optional: register rtk's auto-rewrite hook so the backend CLI sees
# compressed output for common shell commands (git/pytest/cargo/ls).
# Opt-in via RTK_ENABLED=1 so we can A/B against an unmodified peer.
#
# rtk init -g currently only ships an agent target for Claude. Codex has
# no auto-rewrite hook surface upstream — the only win there is from manual
# `rtk <cmd>` wrappers in agent commands, which work without init. We log
# that distinction explicitly so an A/B with no savings is attributed
# correctly.
#
# rtk init -g writes into ${HOME}/.claude — create it first so a fresh
# container does not silently fall into the warn branch.
if [ "${RTK_ENABLED:-0}" = "1" ]; then
  case "${BACKEND:-}" in
    claude)
      mkdir -p "${HOME:-/root}/.claude"
      # Fatal when RTK_ENABLED=1 is explicit: silently falling back to no
      # compression defeats the opt-in and makes A/B results meaningless.
      rtk init -g 2>&1 \
        || { echo "ERROR: rtk init failed with RTK_ENABLED=1; refusing to start performer" >&2; exit 1; }
      ;;
    codex)
      echo "INFO: RTK_ENABLED=1 with BACKEND=codex — rtk has no codex auto-hook; only manual rtk <cmd> wrappers apply" >&2
      ;;
    "")
      echo "INFO: RTK_ENABLED=1 but BACKEND unset — skipping rtk init" >&2
      ;;
    *)
      echo "WARNING: RTK_ENABLED=1 but backend '${BACKEND}' has no supported rtk hook — skipping" >&2
      ;;
  esac
fi

exec python -m performer --serve --port 8088
