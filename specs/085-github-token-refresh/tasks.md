# Tasks: Recover from GitHub auth 401s by refreshing the token

**Input**: Design documents from `/specs/085-github-token-refresh/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/auth-invalidate.md, quickstart.md

**Tests**: TDD is MANDATED by the plan (Constitution Principle II, NON-NEGOTIABLE). Every new
behavior gets a failing unit test FIRST (RED), then minimal code (GREEN), then refactor. Test
tasks below are REQUIRED, not optional.

**Organization**: Tasks are grouped by user story so each story is an independently testable
increment. US1 (P1) is the MVP.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 / US2 / US3 — maps to the user stories in spec.md
- Every task names an exact file path.

## Path Conventions

Single-project layout. Source: `src/coordinare/`. Tests: `tests/unit/`. Run from repo root:
`.venv/bin/pytest …` and `.venv/bin/ruff check …` (never `python -m pytest` — pyenv shim issues).

Confirmed existing test files (reuse, do not create new variants):
- `tests/unit/auth/test_app_auth.py`
- `tests/unit/auth/test_pat_auth.py`
- `tests/unit/services/test_github_service_auth.py`

---

## Phase 1: Setup

- [X] T001 Confirm the test toolchain runs green on the 085 branch before any change: `.venv/bin/pytest tests/unit/auth/test_app_auth.py tests/unit/auth/test_pat_auth.py tests/unit/services/test_github_service_auth.py -q` and capture the baseline pass count (coverage-no-regress gate reference).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The protocol method and the exception type that BOTH the auth impls and the service
classifier/retry depend on. No user story can be implemented until these land.

**TDD note**: T002/T004 are the failing tests; T003/T005 are the minimal code that makes them pass.

- [X] T002 [P] Write a failing test in `tests/unit/auth/test_auth_init.py` (or a new `GitHubAuth` protocol test there) asserting `GitHubAuth` declares an `async def invalidate(self) -> None` member and is still `@runtime_checkable` (Contract A signature). Run it — watch it fail (RED).
- [X] T003 Add `async def invalidate(self) -> None` to the `GitHubAuth` Protocol in `src/coordinare/auth/protocol.py` with a docstring noting it is a safe no-op for static providers. Run T002 — watch it pass (GREEN).
- [X] T004 [P] Write a failing test in `tests/unit/services/test_github_service_auth.py` asserting `AuthGitHubError` exists in `coordinare.services.github`, is a subclass of `PermanentGitHubError` (and therefore of `GitHubError`). Run it — watch it fail (RED).
- [X] T005 Add `class AuthGitHubError(PermanentGitHubError)` to the exception taxonomy in `src/coordinare/services/github.py` (alongside `TransientGitHubError`/`PermanentGitHubError`/`RateLimitedGitHubError`, ~L25-37) with a secret-free docstring. Run T004 — watch it pass (GREEN).

**Checkpoint**: Protocol + exception type exist. User-story phases can now proceed.

---

## Phase 3: User Story 1 — Board polling recovers after a credential goes stale (Priority: P1) 🎯 MVP

**Goal**: On a recognized authorization failure with a refreshable credential, invalidate the
cached credential, mint a fresh one, rebuild the transport, and retry the request exactly once;
a second auth failure becomes one permanent error; the refreshed credential carries forward.

**Independent Test**: Drive `_execute()` against a credential provider whose first minted token is
rejected (auth failure) and whose next token is accepted; verify the operation succeeds with no
operator intervention and the refreshed token is reused on the following call.

### Tests for User Story 1 (write first — RED) ⚠️

- [X] T006 [P] [US1] Failing test in `tests/unit/auth/test_app_auth.py`: `AppAuth.invalidate()` clears `_cached_token`/`_token_expires_at` so the next `get_token()` re-mints a NEW token; `invalidate()` returns `None` and is safe under the existing `_lock` (Contract A1/A3). RED.
- [X] T007 [P] [US1] Failing test(s) in `tests/unit/services/test_github_service_auth.py`: `_execute_request` classification. Auth shapes → `AuthGitHubError`: `TransportServerError(code=401)` (B1); `TransportQueryError` with error `type` ∋ `UNAUTHORIZED` (B2); `aiohttp.ClientResponseError(status=401)` (B3). Non-auth shapes keep their existing classification (FR-009, must NOT become `AuthGitHubError` and must NOT trigger refresh): `TransportServerError(code=502)` → `TransientGitHubError` (B4); `TransportQueryError` with `type` ∋ `FORBIDDEN`/`NOT_FOUND`/`UNPROCESSABLE` (no `UNAUTHORIZED`) → `PermanentGitHubError` (B5); `aiohttp.ClientResponseError(status=429)` → `RateLimitedGitHubError` (B6). RED.
- [X] T008 [P] [US1] Failing test in `tests/unit/services/test_github_service_auth.py`: refresh-and-retry-once success — attempt #1 raises `AuthGitHubError`, fresh token differs, attempt #2 succeeds; `_execute()` returns the attempt-#2 result, `invalidate()` called exactly once, client rebuilt with the fresh token (Contract C1). RED.
- [X] T009 [P] [US1] Failing test in `tests/unit/services/test_github_service_auth.py`: second-auth-failure-is-permanent — both attempts raise `AuthGitHubError` (fresh token differs); `_execute()` raises exactly one `PermanentGitHubError`, no third attempt (Contract C2, FR-004). RED.
- [X] T010 [P] [US1] Failing test in `tests/unit/services/test_github_service_auth.py`: refreshed credential carries forward — after a successful C1 recovery, the next `_execute()` uses the refreshed token, never the stale one (Contract C6, US1 scenario 3). RED.

### Implementation for User Story 1 (GREEN)

- [X] T011 [US1] Implement `AppAuth.invalidate()` in `src/coordinare/auth/app.py`: acquire the existing `_lock`, set `_cached_token = None` and `_token_expires_at = None`, return `None`. Run T006 — GREEN.
- [X] T012 [US1] Fix the misclassifying `except TransportServerError` branch (~L539-544) in `src/coordinare/services/github.py`: route `code == 401` to `AuthGitHubError`; all other codes keep the existing transient classification. Add the `TransportQueryError`→`UNAUTHORIZED`→`AuthGitHubError` path and the defensive `aiohttp.ClientResponseError(status==401)`→`AuthGitHubError` branch. Run T007 — GREEN.
- [X] T013 [US1] Implement refresh-and-retry-once in `_execute()` (~L480-491) in `src/coordinare/services/github.py`, inside the held `_gql_lock`: on `AuthGitHubError` from attempt #1, capture the old token, `await self._auth.invalidate()`, mint via the existing token path; if the fresh token differs, rebuild the client and retry once; a second `AuthGitHubError` raises one `PermanentGitHubError`. Bound: ≤1 invalidate + ≤1 mint + ≤1 retry. Run T008, T009, T010 — GREEN.

**Checkpoint**: US1 is independently testable and complete — the production incident (P1) is fixed. This is the MVP.

---

## Phase 4: User Story 2 — A genuinely invalid credential fails fast and clearly (Priority: P2)

**Goal**: A static (non-refreshable) credential rejected by the server fails immediately with one
permanent error and no retry/loop; auth failures never touch the circuit breaker; transient mint
failures stay transient and 4xx mint failures stay permanent.

**Independent Test**: Configure a static (PAT) credential the server rejects; verify exactly one
permanent error, no retry, and unchanged circuit-breaker/service-health counters.

### Tests for User Story 2 (write first — RED) ⚠️

- [X] T014 [P] [US2] Failing test in `tests/unit/auth/test_pat_auth.py`: `PatAuth.invalidate()` is a safe no-op — returns `None`, raises nothing, and the subsequent `get_token()` returns the same static token (Contract A2). RED.
- [X] T015 [P] [US2] Failing test in `tests/unit/services/test_github_service_auth.py`: static fast-fail — provider whose token is unchanged after `invalidate()`; attempt #1 raises `AuthGitHubError`; `_execute()` raises `PermanentGitHubError` with NO retry and no loop (fresh == old, Contract C3, FR-005). RED.
- [X] T016 [P] [US2] Failing test in `tests/unit/services/test_github_service_auth.py`: breaker/retry integration — an escaping `AuthGitHubError`/`PermanentGitHubError` is ignored by the circuit breaker (`ignore=PermanentGitHubError`, D1) and is NOT retried by stamina (`on=TransientGitHubError` only, D2). RED.
- [X] T017 [P] [US2] Failing test(s) in `tests/unit/services/test_github_service_auth.py`: mint-failure propagation — attempt #1 raises `AuthGitHubError`, then mint raises `TransientGitHubError` → `_execute()` propagates transient (C4, FR-007); mint raises `PermanentGitHubError` (4xx) → propagates permanent (C5, FR-007). RED.

### Implementation for User Story 2 (GREEN)

- [X] T018 [US2] Implement `PatAuth.invalidate()` as a no-op (returns `None`) in `src/coordinare/auth/pat.py`. Run T014 — GREEN.
- [X] T019 [US2] In `_execute()` in `src/coordinare/services/github.py`, ensure the token-equality guard (fresh token == old token → `PermanentGitHubError`, no retry) covers the static-credential path, and confirm mint exceptions from `_current_token()`/`get_token()` propagate unwrapped (transient stays transient, 4xx permanent). Run T015, T016, T017 — GREEN.

**Checkpoint**: US1 + US2 complete — refreshable recovery AND static fast-fail both correct, breaker untouched.

---

## Phase 5: User Story 3 — Operators can see that refresh-and-retry happened (Priority: P3)

**Goal**: Each refresh-and-retry attempt emits a secret-free observability record naming the
action and outcome (`recovered` | `failed`).

**Independent Test**: Trigger a recovering and a still-failing refresh-and-retry; assert a record
names the action + outcome and that no field contains token/header/body text.

### Tests for User Story 3 (write first — RED) ⚠️

- [X] T020 [P] [US3] Failing test in `tests/unit/services/test_github_service_auth.py`: a recovering refresh-and-retry emits `github.auth.refresh_retry` with `outcome="recovered"`; a still-failing one emits `outcome="failed"` (Contract E1, FR-010). RED.
- [X] T021 [P] [US3] Failing test in `tests/unit/services/test_github_service_auth.py`: secret-free assertion — capture every emitted `github.auth.refresh_retry` event and assert no field contains the token string, an `authorization` header value, or response-body text (Contract E2, FR-011). RED.

### Implementation for User Story 3 (GREEN)

- [X] T022 [US3] In the refresh-and-retry path in `_execute()` (`src/coordinare/services/github.py`), emit `logger.info("github.auth.refresh_retry", outcome="recovered"|"failed")` with NO token/header/body fields, on both the recovered and failed branches. Run T020, T021 — GREEN.

**Checkpoint**: All three user stories complete and independently verified.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T023 [P] Run `.venv/bin/ruff check src/coordinare/auth src/coordinare/services/github.py` and fix any lint findings in the touched files.
- [X] T024 Run the full affected suite green and confirm coverage does not regress vs. the T001 baseline: `.venv/bin/pytest tests/unit/auth/test_app_auth.py tests/unit/auth/test_pat_auth.py tests/unit/auth/test_auth_init.py tests/unit/services/test_github_service_auth.py -q`.
- [X] T025 Run the broader github-service regression set to confirm no behavior changed on existing paths: `.venv/bin/pytest tests/unit/services/test_github.py tests/unit/services/test_github_service.py tests/unit/services/test_resilient_github.py tests/unit/services/test_github_coverage.py -q`.
- [X] T026 Validate the quickstart commands in `specs/085-github-token-refresh/quickstart.md` still match reality (test-file names, ruff paths); update quickstart if any path drifted.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: baseline only — no blockers.
- **Foundational (Phase 2)**: depends on Setup. **BLOCKS all user stories** (protocol method + exception type).
- **User Stories (Phase 3-5)**: all depend on Foundational. US1 is the MVP; US2 and US3 build on the same `_execute()` path but are independently testable increments.
- **Polish (Phase 6)**: depends on all desired user stories being implemented.

### User Story Dependencies

- **US1 (P1)**: depends only on Foundational. Delivers the MVP (the production fix).
- **US2 (P2)**: depends on Foundational; shares `_execute()` with US1 (the token-equality guard is added in T013, hardened/tested in T019). Best sequenced after US1.
- **US3 (P3)**: depends on Foundational; the log lines live in the US1 retry path. Sequence after US1.

### Within Each User Story

- Tests (RED) MUST be written and observed failing before implementation (GREEN). TDD Iron Law.
- Foundational T003 (protocol) and T005 (exception) unblock the impl tasks that reference them.

### Parallel Opportunities

- **Phase 2**: T002 and T004 are `[P]` (different files); their impl T003/T005 are also independent.
- **US1 tests**: T006–T010 are `[P]` (T006 in test_app_auth.py; T007–T010 in test_github_service_auth.py — author as distinct test functions, then run together). Impl T011 (app.py) is independent of T012/T013 (github.py); T012 precedes T013 (T013's retry path depends on the classifier).
- **US2 tests**: T014–T017 are `[P]`. Impl T018 (pat.py) is independent of T019 (github.py).
- **US3 tests**: T020–T021 are `[P]`.
- **Polish**: T023 `[P]`; T024/T025 run after all GREEN.

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 (T001 baseline) → Phase 2 (T002-T005 protocol + exception).
2. Phase 3 (T006-T013): RED the US1 tests, then GREEN the AppAuth.invalidate + classifier + retry.
3. **STOP and validate**: US1 independently fixes the production incident — shippable MVP.

### Incremental Delivery

- Add US2 (T014-T019) for static fast-fail + breaker safety + mint-failure propagation.
- Add US3 (T020-T022) for secret-free observability.
- Finish with Polish (T023-T026): lint, coverage-no-regress, broader regression, quickstart check.

Each story is a checkpoint: stop, run its tests, confirm the increment is green before proceeding.
