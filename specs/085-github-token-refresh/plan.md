# Implementation Plan: Recover from GitHub auth 401s by refreshing the token

**Branch**: `085-github-token-refresh` | **Date**: 2026-06-10 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/085-github-token-refresh/spec.md`

## Summary

The coordinare daemon's GraphQL client misclassifies a GitHub **401 Unauthorized**
as a generic transient error and never obtains a fresh credential, so board polling
stops making progress when an installation token goes stale mid-run. This plan adds
a credential **force-invalidate** capability to the auth layer and a bounded
**refresh-and-retry-once** path in the GraphQL client: on a recognized authorization
failure with a refreshable credential, invalidate the cached token, mint a fresh one,
rebuild the transport, and retry exactly once. A static credential (PAT) fails fast
with one permanent error. Authorization failures are classified as permanent so they
never count against the circuit breaker, and a secret-free observability record names
the refresh-and-retry action and its outcome.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv)
**Primary Dependencies**: gql (+ `gql.transport.aiohttp.AIOHTTPTransport`), aiohttp, httpx (AppAuth token mint), stamina (transient retry), structlog (observability), pydantic 2.x (unaffected)
**Storage**: N/A — no persisted state; credential cache is in-memory in `AppAuth`
**Testing**: pytest (`.venv/bin/pytest`), async tests; ruff lint (`.venv/bin/ruff check`)
**Target Platform**: Linux server (long-running async daemon)
**Project Type**: single (src/coordinare + tests/unit)
**Performance Goals**: Zero added latency on the success path (no auth failure → no extra work). On an authorization failure: ≤1 credential mint + ≤1 request retry (bounded, never a loop).
**Constraints**: No secret material (token, authorization header, response body) in any log/record/error; refresh must be shared under concurrency (serialized by the existing `_gql_lock`); fix must not trip or be counted against the circuit breaker.
**Scale/Scope**: Localized change — `auth/protocol.py`, `auth/app.py`, `auth/pat.py`, `services/github.py`. No new modules, no new dependencies.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Change is small, single-responsibility (auth
  invalidation + one classifier branch + one retry path). No new dependencies. Full
  type annotations on the new `invalidate()` protocol method and concrete impls.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS (TDD mandated). Every new
  behavior gets a failing unit test first: protocol/impl `invalidate()`, auth-failure
  classification (both shapes), refresh-and-retry-once, second-401-permanent,
  static-credential fast-fail, breaker-not-counted, secret-free record. Coverage must
  not regress.
- **III. User Experience Consistency** — PASS (N/A UI). Operator-facing error stays a
  clean permanent error with an actionable message; no stack traces leaked; no
  secrets in messages.
- **IV. Performance by Design** — PASS. Budget defined above and mirrored in spec
  SC-002 (≤1 refresh + ≤1 retry, no unbounded loops) and the success path is
  untouched (zero added latency when no auth failure occurs).
- **V. Clarity Before Action** — PASS. Spec has no `NEEDS CLARIFICATION` markers;
  the root cause is precisely diagnosed (github.py `TransportServerError` branch +
  absent invalidate path). Scope boundary is explicit (no broad auth refactor).

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/085-github-token-refresh/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (internal interface contracts)
│   └── auth-invalidate.md
├── checklists/
│   └── requirements.md  # spec quality checklist (already created)
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── auth/
│   ├── protocol.py      # GitHubAuth Protocol — ADD: async def invalidate()
│   ├── app.py           # AppAuth — ADD: invalidate() clears _cached_token/_token_expires_at
│   └── pat.py           # PatAuth — ADD: invalidate() no-op (static token)
└── services/
    └── github.py        # ADD: AuthGitHubError(PermanentGitHubError);
                         #      classify 401 / UNAUTHORIZED into it;
                         #      refresh-and-retry-once in _execute(); secret-free log

tests/unit/
├── test_auth_app.py        # AppAuth.invalidate() clears cache; next get_token re-mints
├── test_auth_pat.py        # PatAuth.invalidate() is a safe no-op
└── test_github_service.py  # classification (both shapes), refresh+retry-once,
                            # second-401-permanent, static fast-fail, breaker untouched,
                            # secret-free observability
```

**Structure Decision**: Single-project layout (existing). The change is confined to
the auth package (credential lifecycle) and the GraphQL service (error classification
+ retry orchestration). Test files mirror existing `tests/unit/` conventions
(class-based where the module already uses them; async tests via the project's pytest
async config). Exact existing test-file names are confirmed during `/speckit.tasks`.

## Design Overview (informs Phase 1)

1. **Protocol extension** — `GitHubAuth.invalidate()` (async, returns None): force the
   provider to discard any cached credential so the next `get_token()` produces a fresh
   one. Documented as a safe no-op for static providers.
2. **AppAuth.invalidate()** — under its existing `_lock`, set `_cached_token = None` and
   `_token_expires_at = None`. Next `get_token()` takes the mint path.
3. **PatAuth.invalidate()** — no-op (static token is genuinely permanent; refreshing
   cannot help, and looping must be impossible).
4. **AuthGitHubError(PermanentGitHubError)** — a dedicated subclass so an auth failure
   (a) is recognized by the retry orchestrator, (b) is *permanent* if it escapes
   (static credential / second 401), and (c) is automatically ignored by the circuit
   breaker (which already `ignore=PermanentGitHubError`) — satisfying FR-005/FR-006
   with no breaker changes.
5. **Classification (two shapes, FR-001)** in `_execute_request`:
   - transport-level non-2xx: `TransportServerError` with `code == 401` (and the
     defensive `aiohttp.ClientResponseError` `status == 401` branch) → `AuthGitHubError`;
     other codes keep existing classification (5xx→transient, etc.).
   - structured response-level: `TransportQueryError` whose error types include
     `UNAUTHORIZED` → `AuthGitHubError`. `FORBIDDEN`/`NOT_FOUND`/`UNPROCESSABLE` stay
     permanent (missing scope / bad node — refresh cannot fix them).
6. **Refresh-and-retry-once (FR-002/003/004/008)** in `_execute`, inside the held
   `_gql_lock` (so concurrent callers share one refresh — FR-008): on `AuthGitHubError`
   from the first attempt, `await self._auth.invalidate()`, mint a fresh token via
   `_current_token()`. If the new token equals the old one (static credential or
   unchanged), raise `PermanentGitHubError` (no retry — FR-005). Otherwise rebuild the
   client and retry once; a second `AuthGitHubError` becomes a single
   `PermanentGitHubError` (FR-004). A transient failure while minting (`TransientGitHubError`
   from `_fetch_installation_token` 5xx) propagates as transient; a 4xx mint stays
   permanent (FR-007) — both fall out naturally without extra handling.
7. **Observability (FR-010/011)** — emit `logger.info("github.auth.refresh_retry",
   outcome="recovered"|"failed")` with NO token/header/body fields. Existing
   structlog redaction conventions apply; tests assert no secret substrings.

## Complexity Tracking

> No Constitution violations. Table intentionally empty.
