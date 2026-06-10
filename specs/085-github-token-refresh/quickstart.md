# Quickstart: GitHub auth 401 token-refresh recovery

## What changed

When the coordinare's GitHub credential goes stale mid-run, the GraphQL client now
**invalidates the cached credential, mints a fresh one, and retries the request once**
instead of misclassifying the 401 as transient and silently starving board polling.

- **App auth (refreshable)**: recovers automatically within the same poll cycle.
- **PAT auth (static)**: fails fast with one clear permanent error (no pointless retry).
- Auth failures never trip the circuit breaker; no secrets ever appear in logs.

## Developer notes

- New protocol method: `GitHubAuth.invalidate()` — force-discard the cached credential.
  - `AppAuth.invalidate()` clears `_cached_token` / `_token_expires_at`.
  - `PatAuth.invalidate()` is a no-op.
- New exception: `AuthGitHubError(PermanentGitHubError)` in
  `coordinare.services.github` — recognized auth-failure shape; permanent if it escapes.
- Recovery is bounded: ≤1 invalidate + ≤1 mint + ≤1 retry per request. Never loops.

## Run the tests

```bash
# from repo root
.venv/bin/pytest tests/unit/auth/test_app_auth.py tests/unit/auth/test_pat_auth.py \
                 tests/unit/services/test_github_service_auth.py -q
.venv/bin/ruff check src/coordinare/auth src/coordinare/services/github.py
```

(Exact test-file names confirmed in `/speckit.tasks`; the suite must stay green and
coverage must not regress.)

## Manual sanity (optional, live)

With App auth configured and the daemon running, a transient 401 from GitHub should now
produce a single `github.auth.refresh_retry outcome=recovered` log line and board
polling continues — instead of a `GraphQL HTTP 401` transient error and stalled
dispatch. Confirm no token/header/body text appears in that log line.
```bash
set -a && source .env && set +a   # required before launching coordinare
```
