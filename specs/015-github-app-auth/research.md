# Research: GitHub App Auth, Webhook HMAC, Daemon Signalling

**Branch**: `015-github-app-auth` | **Date**: 2026-03-12 | **Phase**: 0

---

## 1. JWT Library Choice for GitHub App Authentication

**Decision**: Use `PyJWT[crypto]>=2.8`

**Rationale**:
- PyJWT is the de-facto standard Python JWT library; `[crypto]` extra pulls in `cryptography`, which provides RSA signing via `RS256`.
- GitHub's installation token endpoint requires a JWT signed with `RS256` using the app's RSA private key. PyJWT handles this in two lines: `jwt.encode(payload, private_key, algorithm="RS256")`.
- `cryptography` is already transitively present via several existing dependencies (httpx, aiohttp TLS); adding `PyJWT[crypto]` adds only the JWT library itself.
- **Alternatives rejected**:
  - `python-jose[cryptography]` — unmaintained (last release 2022), open CVEs.
  - Manual RSA + base64 — several hundred lines of error-prone code; no benefit.
  - `authlib` — full OAuth2 framework, far heavier than needed; spec out of scope for OAuth user tokens.

**JWT payload for GitHub Apps**:
```
{
  "iss": "<app_id>",           # GitHub App ID (integer, sent as string)
  "iat": now_utc - 60s,        # issued-at (backdate 60s for clock skew)
  "exp": now_utc + 600s        # expiry (max 10 min; GitHub rejects >10 min)
}
algorithm = RS256
key       = RSA private key (PEM file)
```

**Installation token exchange**:
```
POST https://api.github.com/app/installations/{installation_id}/access_tokens
Authorization: Bearer <signed-jwt>
Accept: application/vnd.github+json
```
Response contains `token` (bearer string) and `expires_at` (ISO 8601 UTC).

**Token lifecycle**:
- Tokens are valid for **1 hour** from issuance.
- AppAuth caches the token and its `expires_at`.
- Token is considered stale when `expires_at - now < 5 minutes` (FR-003).
- On stale detection, AppAuth generates a fresh JWT and re-calls the exchange endpoint.
- `get_token()` is therefore an `async` method — it may need to `await` an HTTP call.

---

## 2. Webhook HMAC-SHA256 Verification

**Decision**: Use stdlib `hmac` + `hashlib` — **zero new dependencies**.

**Rationale**:
- GitHub sends `X-Hub-Signature-256: sha256=<hex-digest>` on every webhook POST.
- Python stdlib `hmac.compare_digest(expected, received)` performs constant-time comparison (prevents timing attacks).
- FastAPI provides the raw request body via `await request.body()`.

**Verification pattern**:
```python
import hashlib, hmac

def _verify_signature(body: bytes, secret: str, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])
```

**Endpoint registration**: Mount on existing dashboard FastAPI application (`dashboard_port: 8090`) at configurable `path` (default `/webhook/github`). No second HTTP server.

**Rejection response**: HTTP 401 with structured JSON body; log at `warning` level with structured fields (SC-005).

---

## 3. Daemon Polling Loop — Webhook Trigger Signal

**Decision**: Add `_webhook_trigger: asyncio.Event` to `CoordinareDaemon`; replace bare `await self._sleep(N)` calls with an `asyncio.wait_for` / `asyncio.wait` pattern.

**Current code** (lines 429, 454 of `daemon.py`):
```python
await self._sleep(self._poll_interval_seconds)
```

**Replacement when polling is enabled (interval > 0)**:
```python
# Wake early if a webhook fires, but still respect the configured interval
with contextlib.suppress(asyncio.TimeoutError):
    await asyncio.wait_for(self._webhook_trigger.wait(), self._poll_interval_seconds)
self._webhook_trigger.clear()
```
This blocks for up to `interval_seconds` but wakes immediately when a webhook arrives.

**When polling is disabled (interval = 0)**:
```python
# Block indefinitely until a webhook fires (or stop is requested)
await self._webhook_trigger.wait()
self._webhook_trigger.clear()
```
The daemon loop body still runs on each trigger; polling is simply never scheduled.

**Webhook handler** sets the event:
```python
self._webhook_trigger.set()   # wakes the daemon loop
```
Concurrency requirement (FR-011): The event is only `set()`; the daemon only `clear()`s it after waking. A webhook arriving while a cycle is running will be buffered by the event and trigger the next cycle — no concurrent cycles.

**Sleep function injection**: `CoordinareDaemon.__init__` accepts `sleep_func` for testability. The new pattern no longer calls `sleep_func` directly when a webhook event is used; tests that need deterministic control should inject a `asyncio.Event` mock or use `sleep_func=asyncio.sleep` (the default, fine for integration tests).

---

## 4. Config Model Changes

**`poll_interval_seconds`**:
- Current: `Field(default=30, ge=10, le=300)`
- Required: `Field(default=30, ge=0, le=3600)` — `0` means disabled, upper bound raised for webhook-only deployments that still want a slow background poll.
- `_validate_retry_backoff_caps`: guard `if poll == 0: return self` before the backoff comparison (otherwise division/comparison with `0` is misleading).

**`github_token`**:
- Current: `SecretStr` (required)
- Required: `SecretStr | None = None`
- Validation: required only when `github_auth == "pat"` (model validator, not field validator).

**New fields on `ProjectConfiguration`** (flat, prefixed with `github_`):
```
github_auth: Literal["pat", "app"] = "pat"
github_app_id: int | None = None
github_private_key_path: Path | None = None
github_installation_id: int | None = None
```

**New top-level nested models** (mirrors spec config surface):
```
polling: PollingConfig = PollingConfig()      # wraps poll_interval_seconds
webhooks: WebhookConfig = WebhookConfig()    # enabled, secret, path
```

**Design decision — flat vs. nested for `polling.interval_seconds`**:
The existing `poll_interval_seconds` flat field and `COORDINARE_POLL_INTERVAL_SECONDS` env var are used throughout the codebase. Rather than breaking the flat layout, `PollingConfig` is a thin wrapper added for spec alignment; `ProjectConfiguration.poll_interval_seconds` is retained as the primary accessor (model validator copies `polling.interval_seconds` into it after load). This preserves backward-compat and avoids renaming every callsite in daemon.py.

---

## 5. GitHubService Token Injection Pattern

**Current**: `GitHubService.__init__(self, token: str, ...)` — token is a plain string, baked into GQL transport.

**Required**: Accept a `GitHubAuth` (protocol/callable) so the service can fetch a fresh token on each request cycle.

**Approach**: Add `async def _get_token(self) -> str` that delegates to `self._auth.get_token()`. Rebuild the GQL client when the token differs from the last used token. Since the GQL transport is constructed with the `Authorization` header, token rotation requires a new transport instance (cheap — no persistent connection).

**Backward compatibility**: `PatAuth` wraps a static `SecretStr`; `get_token()` returns the value immediately. Callers that currently pass a plain string to `GitHubService` are all in `__main__.py` / `coordinare.py` — they will be updated to construct the appropriate `Auth` object based on config.

---

## 6. Startup Validation Gates

All validation happens in `ProjectConfiguration` model validators and a dedicated `validate_auth_config(cfg)` function called at startup:

| Auth mode | Required fields | Error if missing |
|-----------|-----------------|------------------|
| `pat`     | `github_token`  | "github.auth=pat requires github.token" |
| `app`     | `github_app_id`, `github_private_key_path`, `github_installation_id` | specific field name in error message |

Additionally:
- If `github_private_key_path` is set but the file does not exist or is not readable → startup error.
- If `polling.interval_seconds == 0` and `webhooks.enabled == False` → log `WARNING` at startup (FR-013); do NOT prevent startup (spec: "starts successfully").

---

## 7. Dependency Addition

Add to `pyproject.toml` `[project.dependencies]`:
```
PyJWT[crypto]>=2.8
```

No other new dependencies. `httpx` (already present) is used for the installation token HTTP call (REST endpoint, not GQL).
