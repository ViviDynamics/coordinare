# Phase 1 Data Model: GitHub auth 401 token-refresh recovery

This feature introduces no persisted data. The "entities" are in-memory behavioral
contracts and one new exception type.

## Credential provider (`GitHubAuth` protocol)

The shared interface every auth backend implements. Extended with one operation.

| Member | Signature | Behavior |
|--------|-----------|----------|
| `get_token` (existing) | `async () -> str` | Return a current, valid bearer token. App mode mints/caches; PAT mode returns the static token. |
| `invalidate` (**new**) | `async () -> None` | Force-discard any cached credential so the next `get_token()` produces a fresh one. **Safe no-op for static providers.** |

**Implementations**
- **AppAuth** (refreshable): `invalidate()` acquires the existing `_lock` and sets
  `_cached_token = None`, `_token_expires_at = None`. Next `get_token()` takes the mint
  path. State fields: `_cached_token: str | None`, `_token_expires_at: float | None`
  (monotonic seconds).
- **PatAuth** (static): `invalidate()` is a no-op. `get_token()` always returns the
  same `_token`.

**Invariant**: After `invalidate()`, a refreshable provider must NOT return its
previously cached token from the next `get_token()`. A static provider legitimately
returns the same token (this is how the service detects "cannot refresh").

## Authorization failure (`AuthGitHubError`)

New exception, subclass of `PermanentGitHubError` (itself `GitHubError(RuntimeError)`).

| Aspect | Value |
|--------|-------|
| Parent | `PermanentGitHubError` |
| Raised when | first request attempt fails with a recognized auth shape (see below) |
| If it escapes | treated as permanent: not retried by stamina (`on=TransientGitHubError`), ignored by the circuit breaker (`ignore=PermanentGitHubError`) |
| Message | actionable, secret-free (no token/header/body) |

**Recognized shapes (the two FR-001 shapes)**
1. Transport-level non-2xx: `TransportServerError` with `code == 401` (and defensive
   `aiohttp.ClientResponseError` with `status == 401`).
2. Response-level: `TransportQueryError` whose error `type` set contains `UNAUTHORIZED`.

Non-auth codes/types retain their current classification:
`403/FORBIDDEN`, `404/NOT_FOUND`, `UNPROCESSABLE` → permanent (non-auth);
`429` → rate-limited; `≥500` → transient.

## Recovery event (observability record)

Emitted once per refresh-and-retry attempt.

| Field | Example | Notes |
|-------|---------|-------|
| event | `github.auth.refresh_retry` | fixed event name |
| outcome | `recovered` \| `failed` | recovered = retry succeeded; failed = second attempt still auth-failed |

**Forbidden fields (FR-011)**: no `token`, `authorization`, header, or response-`body`
content in any field.

## State transition — a single request through the auth-recovery path

```text
attempt #1
  ├─ success                         → return result            (happy path, no event)
  ├─ non-auth error                  → existing classification  (no refresh, no event)
  └─ AuthGitHubError
       └─ invalidate() + mint fresh token
            ├─ fresh == old (static / unchanged) → PermanentGitHubError, event=failed, NO retry
            └─ fresh != old → rebuild client, attempt #2
                 ├─ success            → return result, event=recovered
                 └─ AuthGitHubError    → PermanentGitHubError, event=failed   (FR-004: exactly one retry)
       └─ mint raises TransientGitHubError (5xx)  → propagate transient        (FR-007)
       └─ mint raises PermanentGitHubError (4xx)  → propagate permanent        (FR-007)
```

Bound: at most one `invalidate()` + one mint + one retry per originating request
(SC-002). Concurrency: the whole transition runs under the existing `_gql_lock`, so
concurrent callers share the single refresh (FR-008).
