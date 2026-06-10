# Phase 0 Research: GitHub auth 401 token-refresh recovery

No `NEEDS CLARIFICATION` markers existed in the spec; the root cause was diagnosed
directly from the code. The research below records the decisions and the evidence
each rests on.

## R1 — How does a stale GitHub credential surface in the GraphQL client?

**Decision**: Recognize two failure shapes as authorization failures:
(a) transport-level `TransportServerError` with `code == 401` (and defensively
`aiohttp.ClientResponseError` with `status == 401`), and (b) response-level
`TransportQueryError` whose GraphQL error `type` set includes `UNAUTHORIZED`.

**Rationale**: `src/coordinare/services/github.py:539-544` catches
`TransportServerError` and unconditionally raises `TransientGitHubError(f"GraphQL HTTP
{code}: {exc}")` regardless of `code`. gql's aiohttp transport raises
`TransportServerError(code=401)` when GitHub returns a 401 with a non-GraphQL body
(e.g. `{"message":"Bad credentials"}`). So a 401 is misclassified as transient —
bypassing the real auth handling (`TransportQueryError` + `UNAUTHORIZED` →
`PermanentGitHubError` at L529-536; `aiohttp.ClientResponseError` non-5xx →
`PermanentGitHubError` at L515). Both shapes must route to the new auth-failure path.

**Alternatives considered**: Treat *only* `TransportServerError(401)` as auth (matches
the single observed incident). Rejected — FR-001 requires recognizing both shapes, and
the response-level `UNAUTHORIZED` shape is the documented GitHub behavior when the body
*is* GraphQL-shaped; covering both is cheap and complete.

## R2 — Why does a retry alone not fix it?

**Decision**: A retry must be paired with credential invalidation; retrying with the
same cached token cannot recover.

**Rationale**: `_DEFAULT_RETRY_KWARGS["attempts"] = 1` (no real retry today), and
`_execute()` only rebuilds the gql client when `token != self._last_token`
(github.py:488). `AppAuth.get_token()` serves a cached token until
`REFRESH_BUFFER_SECONDS = 300` before expiry (app.py:127-149) with no force-invalidate
path. So even a retry reuses the rejected token. The fix needs an explicit
`invalidate()` so the next `get_token()` mints a fresh credential, then a single retry
that actually carries it.

**Alternatives considered**: Shrink `REFRESH_BUFFER_SECONDS` / proactively refresh on a
timer. Rejected — does not address rejection *before* the buffer trips (clock skew,
revocation, auth-edge blips) and adds needless token churn on the happy path.

## R3 — How to keep auth failures off the circuit breaker?

**Decision**: Model the auth failure as `AuthGitHubError(PermanentGitHubError)`.

**Rationale**: `_guarded_execute` already guards with
`ignore=PermanentGitHubError` (github.py:572) precisely so application-layer errors
(auth, ruleset, missing node) don't trip the breaker, and `stamina.retry(on=
TransientGitHubError)` won't retry a permanent error. Subclassing `PermanentGitHubError`
gives FR-006 (not counted against breaker) and FR-004/FR-005 (escaping auth failure is
permanent) for free — no breaker or stamina changes.

**Alternatives considered**: A standalone `AuthGitHubError(GitHubError)` not under the
permanent branch. Rejected — would require touching the breaker `ignore` set and the
stamina `on` set, widening the change for no benefit.

## R4 — Where does the refresh-and-retry loop live, and how is concurrency handled?

**Decision**: Inside `_execute()`, within the already-held `_gql_lock`. On
`AuthGitHubError` from the first attempt: `await self._auth.invalidate()`, mint a fresh
token; if it equals the old token → `PermanentGitHubError` (static / unchanged, no
retry); else rebuild client and retry once; a second `AuthGitHubError` →
`PermanentGitHubError`.

**Rationale**: `_gql_lock` already serializes the whole request (github.py:486, added in
061 because `AIOHTTPTransport` can't service concurrent `execute_async`). Doing the
refresh under that same lock means concurrent callers cannot each mint — the first
refreshes and caches, the rest (serialized) observe the fresh token via the
`token != self._last_token` rebuild check. That satisfies FR-008 (shared refresh) with
no new lock. Keeping the loop in `_execute` (below stamina, below the breaker) means the
single retry is invisible to both and cannot compound into an unbounded loop (FR-004/
SC-002).

**Alternatives considered**: Implement the retry in `_retried_execute` via stamina
`attempts`. Rejected — stamina retries the whole `_execute` (re-acquiring the lock,
re-reading the token) and conflates transient-network retry policy with auth recovery;
also harder to bound to exactly one auth retry and to share the refresh.

## R5 — Static credential (PAT) behavior

**Decision**: `PatAuth.invalidate()` is a no-op; an auth failure with a static
credential raises one `PermanentGitHubError` and does not retry.

**Rationale**: A static PAT cannot be refreshed; retrying is pointless and a loop must
be impossible (FR-005). The "new token == old token" check after `invalidate()` detects
this generically (the no-op leaves `get_token()` returning the same string), so PAT and
"refresh produced an identical token" collapse to the same safe permanent path.

**Alternatives considered**: Branch on provider type (`isinstance(auth, PatAuth)`).
Rejected — leaks provider identity into the service and breaks the Protocol abstraction;
the token-equality check is provider-agnostic and also covers the degenerate App case.

## R6 — Mint failure during refresh (FR-007)

**Decision**: Let mint failures propagate with their existing classification.

**Rationale**: `AppAuth._fetch_installation_token()` already raises
`TransientGitHubError` on 5xx / malformed response and `PermanentGitHubError` on 4xx
(app.py:106-125). The refresh path calls `_current_token()` → `get_token()` → mint, so a
transient mint failure surfaces as transient (retryable by the outer stamina) and a 4xx
mint as permanent — FR-007 holds with zero extra code.

## R7 — Observability without secrets (FR-010/FR-011)

**Decision**: Emit `logger.info("github.auth.refresh_retry", outcome=...)` with
`outcome` ∈ {`recovered`, `failed`} and no token/header/body fields.

**Rationale**: structlog is already the service logger (github.py:18). Naming the action
and a coarse outcome gives operators correlation without any secret material. Tests
assert the emitted event contains no token/authorization/body substrings (FR-011).
