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

exec python -m performer --serve --port 8088
