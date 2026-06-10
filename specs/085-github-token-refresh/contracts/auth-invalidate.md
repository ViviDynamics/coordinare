# Internal Interface Contract: credential invalidation + auth-failure recovery

This feature has no external API surface. The contracts below are the internal
interfaces between the GraphQL service and the auth layer. Each row is a testable
assertion driving a unit test in Phase 2.

## Contract A — `GitHubAuth.invalidate()`

```python
async def invalidate(self) -> None: ...
```

| ID | Given | When | Then |
|----|-------|------|------|
| A1 | `AppAuth` with a cached token | `invalidate()` then `get_token()` | the second `get_token()` mints a NEW token (cache was cleared) — `_cached_token`/`_token_expires_at` reset to `None` |
| A2 | `PatAuth(token="x")` | `invalidate()` then `get_token()` | returns `"x"` unchanged; `invalidate()` raises nothing (no-op) |
| A3 | any provider | `invalidate()` | returns `None`; does not raise; concurrent calls are safe (AppAuth uses its existing `_lock`) |

## Contract B — auth-failure classification in `_execute_request`

| ID | Given (raised by transport) | Then |
|----|------------------------------|------|
| B1 | `TransportServerError` with `code == 401` | raises `AuthGitHubError` |
| B2 | `TransportQueryError` with error `type` ∋ `UNAUTHORIZED` | raises `AuthGitHubError` |
| B3 | `aiohttp.ClientResponseError` with `status == 401` | raises `AuthGitHubError` |
| B4 | `TransportServerError` with `code == 502` | raises `TransientGitHubError` (unchanged) |
| B5 | `TransportQueryError` with `type` ∋ `FORBIDDEN`/`NOT_FOUND`/`UNPROCESSABLE` (no `UNAUTHORIZED`) | raises `PermanentGitHubError` (unchanged, NOT auth) |
| B6 | `aiohttp.ClientResponseError` with `status == 429` | raises `RateLimitedGitHubError` (unchanged) |

`AuthGitHubError` is a subclass of `PermanentGitHubError`, so B1–B3 also satisfy
"permanent if it escapes" and "ignored by the circuit breaker".

## Contract C — refresh-and-retry-once in `_execute`

| ID | Given | When | Then |
|----|-------|------|------|
| C1 | refreshable provider; attempt #1 → `AuthGitHubError`; fresh token differs; attempt #2 → success | `_execute()` | returns the attempt-#2 result; `invalidate()` called once; client rebuilt with fresh token; observability `outcome=recovered` |
| C2 | refreshable provider; both attempts → `AuthGitHubError` (fresh token differs) | `_execute()` | raises exactly one `PermanentGitHubError`; no third attempt; observability `outcome=failed` |
| C3 | static provider (PAT); attempt #1 → `AuthGitHubError` | `_execute()` | raises `PermanentGitHubError`; NO retry (fresh == old); no loop |
| C4 | refreshable provider; attempt #1 → `AuthGitHubError`; mint raises `TransientGitHubError` | `_execute()` | propagates `TransientGitHubError` (retryable upstream) |
| C5 | refreshable provider; attempt #1 → `AuthGitHubError`; mint raises `PermanentGitHubError` (4xx) | `_execute()` | propagates `PermanentGitHubError` |
| C6 | next call after a successful C1 recovery | `_execute()` | carries the refreshed token (stale token never reused) |

## Contract D — circuit breaker + retry integration

| ID | Given | Then |
|----|-------|------|
| D1 | an escaping `AuthGitHubError`/`PermanentGitHubError` from auth | NOT counted against the circuit breaker (`ignore=PermanentGitHubError`) |
| D2 | an `AuthGitHubError` | NOT retried by `stamina` (`on=TransientGitHubError` only) — the single retry is internal to `_execute` |

## Contract E — observability is secret-free

| ID | Given | Then |
|----|-------|------|
| E1 | any `github.auth.refresh_retry` event (recovered or failed) | event names the action + outcome |
| E2 | any emitted event/log/error message on these paths | contains no token, no `Authorization` header value, no response body text |
