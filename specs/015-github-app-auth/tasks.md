# Tasks: GitHub App Auth, Polling Config & Webhooks

**Input**: Design documents from `/specs/015-github-app-auth/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add the one new dependency and scaffold the new `auth/` module.

- [X] T001 Add `PyJWT[crypto]>=2.8` to `[project.dependencies]` in `pyproject.toml`
- [X] T002 Create `src/coordinare/auth/` package skeleton: `__init__.py`, `protocol.py`, `pat.py`, `app.py` (empty files with module docstrings)
- [X] T003 Create `tests/unit/auth/` test package: `__init__.py`, `test_pat_auth.py`, `test_app_auth.py` (empty stubs)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core protocol definition and config model changes that ALL user story implementations depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 Define `GitHubAuth` Protocol with `async get_token() -> str` in `src/coordinare/auth/protocol.py`
- [X] T005 [P] Add `github_auth: Literal["pat", "app"] = "pat"` to `ProjectConfiguration` in `src/coordinare/config.py`
- [X] T006 [P] Add `github_app_id: int | None = None`, `github_private_key_path: Path | None = None`, `github_installation_id: int | None = None` to `ProjectConfiguration` in `src/coordinare/config.py`
- [X] T007 [P] Change `github_token` from `SecretStr` (required) to `SecretStr | None = None` in `ProjectConfiguration` in `src/coordinare/config.py`
- [X] T008 [P] Change `poll_interval_seconds` field constraints from `ge=10, le=300` to `ge=0, le=3600` in `ProjectConfiguration` in `src/coordinare/config.py`
- [X] T009 Add `PollingConfig` and `WebhookConfig` Pydantic models to `src/coordinare/config.py` (`PollingConfig.interval_seconds: int`, `WebhookConfig.enabled/secret/path`)
- [X] T010 Add `webhooks: WebhookConfig = WebhookConfig()` field to `ProjectConfiguration` in `src/coordinare/config.py`
- [X] T011 Add `_validate_auth_config` model validator to `ProjectConfiguration` in `src/coordinare/config.py` (PAT requires token; App requires app_id + private_key_path + installation_id)
- [X] T012 Update `_validate_retry_backoff_caps` model validator in `src/coordinare/config.py` to guard `if self.poll_interval_seconds == 0: return self` before the backoff comparison
- [X] T013 Add `_validate_webhook_config` model validator to `ProjectConfiguration` in `src/coordinare/config.py` (secret required when enabled=True)
- [X] T014 Add `_validate_github_token` field validator removal / replacement: remove the now-obsolete field-level validator for `github_token` from `src/coordinare/config.py` (non-None check moved into `_validate_auth_config`)
- [X] T015 Write unit tests for all new config validators in `tests/unit/config/test_config_auth.py` (PAT requires token, App requires all three fields, poll=0 skips backoff check, webhook secret required when enabled)

**Checkpoint**: Config model is complete and tested. Auth protocol is defined. All user story phases may now begin.

---

## Phase 3: User Story 2 — PAT Mode Backwards Compatibility (Priority: P1) 🎯 MVP

**Goal**: Existing PAT-based configs continue to work unchanged after the auth refactor. GitHubService accepts a `GitHubAuth` instead of a raw string token.

**Independent Test**: Start Coordinare with a config containing only `github_token` (no `github_auth` field). Verify it initialises and interacts with GitHub identically to before.

- [X] T016 [US2] Implement `PatAuth` class in `src/coordinare/auth/pat.py` (stores `SecretStr`; `get_token()` returns `token.get_secret_value()` immediately; raises `ValueError` at construction if token is empty)
- [X] T017 [US2] Implement `build_auth(config)` factory (PAT branch only) in `src/coordinare/auth/__init__.py`; export `GitHubAuth`, `PatAuth`, `build_auth`
- [X] T018 [US2] Refactor `GitHubService.__init__` to accept `auth: GitHubAuth` instead of `token: str` in `src/coordinare/services/github.py`
- [X] T019 [US2] Add `_last_token: str | None = None` and `async def _current_token(self) -> str` to `GitHubService` in `src/coordinare/services/github.py`; rebuild `self._client` only when `_current_token()` returns a different value than `_last_token`
- [X] T020 [US2] Update `_bootstrap_services()` in `src/coordinare/__main__.py` to call `build_auth(config)` and pass the `GitHubAuth` object to `GitHubService` (replacing `token=config.github_token.get_secret_value()`)
- [X] T021 [P] [US2] Write unit tests for `PatAuth` in `tests/unit/auth/test_pat_auth.py` (get_token returns token; empty token raises ValueError at construction)
- [X] T022 [P] [US2] Write unit tests for `GitHubService` token rotation in `tests/unit/services/test_github_service_auth.py` (client rebuilt when token changes; not rebuilt when token unchanged)

**Checkpoint**: PAT mode fully functional. All existing configs work. `GitHubService` uses `GitHubAuth` protocol.

---

## Phase 4: User Story 1 — GitHub App Mode (Priority: P1)

**Goal**: Coordinare can run as a GitHub App installation, generating JWTs, exchanging them for installation tokens, and refreshing automatically before expiry.

**Independent Test**: Configure `github_auth: app` with valid app credentials. Start and run for >1 hour. Verify all GitHub interactions appear under the bot identity and no credential errors occur.

- [X] T023 [US1] Implement `AppAuth._load_key(path)` static method in `src/coordinare/auth/app.py`: reads PEM bytes from disk; raises `ValueError` with actionable message if file does not exist or is unreadable
- [X] T024 [US1] Implement `AppAuth.__init__` in `src/coordinare/auth/app.py`: stores `_app_id`, `_installation_id`, calls `_load_key`, initialises `_cached_token = None`, `_token_expires_at = None`, `_lock = None`
- [X] T025 [US1] Implement `AppAuth._make_jwt()` in `src/coordinare/auth/app.py`: builds `{"iss": str(app_id), "iat": now-60, "exp": now+600}` payload and signs with `PyJWT` using `RS256`
- [X] T026 [US1] Implement `AppAuth._fetch_installation_token()` in `src/coordinare/auth/app.py`: `POST /app/installations/{id}/access_tokens` via `httpx.AsyncClient`; parses `token` and `expires_at` from JSON response; converts `expires_at` ISO 8601 to monotonic seconds; raises `TransientGitHubError` on HTTP error
- [X] T027 [US1] Implement `AppAuth.get_token()` in `src/coordinare/auth/app.py`: lazy-initialise `asyncio.Lock`; check cache validity (>5 min remaining); double-checked locking → `_fetch_installation_token()` if stale; return cached token
- [X] T028 [US1] Add `AppAuth` branch to `build_auth()` factory in `src/coordinare/auth/__init__.py`; export `AppAuth`
- [X] T029 [US1] Add `validate_auth_config(config)` startup function to `src/coordinare/auth/__init__.py` that verifies `github_private_key_path` exists and is readable on disk (raises `SystemExit` with actionable message)
- [X] T030 [US1] Call `validate_auth_config(config)` in `src/coordinare/__main__.py` after config load, before `_bootstrap_services()`
- [X] T031 [P] [US1] Write unit tests for `AppAuth` JWT generation in `tests/unit/auth/test_app_auth.py`: JWT contains correct `iss`, `iat` within 60-s window, `exp` ≤ 10 min, `alg=RS256`
- [X] T032 [P] [US1] Write unit tests for `AppAuth` token caching + refresh in `tests/unit/auth/test_app_auth.py`: cached token returned when valid; refresh triggered when <5 min remaining; concurrent callers serialised by lock; `_load_key` raises `ValueError` on missing file; transient HTTP error surfaces as `TransientGitHubError`

**Checkpoint**: App mode fully functional. JWT generation, token exchange, and auto-refresh all work and are unit-tested.

---

## Phase 5: User Story 4 — Configurable Polling (Priority: P2)

**Goal**: `poll_interval_seconds: 0` disables all timer-based polling. Non-zero values control the cycle cadence. Daemon loop correctly handles both modes.

**Independent Test**: Set `poll_interval_seconds: 0`. Verify Coordinare starts, logs "polling disabled", and schedules no automatic cycles. Set `poll_interval_seconds: 120` and verify cycles fire at the configured cadence.

- [X] T033 [US4] Add `_webhook_trigger: asyncio.Event` attribute to `CoordinareDaemon.__init__` in `src/coordinare/daemon.py` (accept optional `webhook_trigger: asyncio.Event | None = None` parameter; default to `asyncio.Event()`)
- [X] T034 [US4] Replace the `await self._sleep(self._poll_interval_seconds)` call at line 429 in `src/coordinare/daemon.py` with `wait_for_next_cycle` logic: when `poll > 0`, use `asyncio.wait_for(self._webhook_trigger.wait(), poll)` (suppress `TimeoutError`) then `self._webhook_trigger.clear()`; when `poll == 0`, block on `asyncio.wait([trigger.wait(), stop_event.wait()], FIRST_COMPLETED)` then clear trigger
- [X] T035 [US4] Apply the same `wait_for_next_cycle` replacement at the second sleep call (line 454, after `CircuitOpenError`) in `src/coordinare/daemon.py`
- [X] T036 [US4] Add structured log `logger.info("polling_disabled")` at daemon startup when `self._poll_interval_seconds == 0` in `src/coordinare/daemon.py`
- [X] T037 [US4] Add startup warning `logger.warning("no_trigger_source_configured")` in `src/coordinare/__main__.py` when `config.poll_interval_seconds == 0` and `not config.webhooks.enabled`
- [X] T038 [US4] Write unit tests for daemon polling changes in `tests/unit/daemon/test_daemon_webhook.py`: `poll=0` blocks on trigger event; `poll>0` wakes early when trigger is set; trigger is cleared after each wake; stop event unblocks poll=0 loop

**Checkpoint**: Polling disable and configurable interval both work. Daemon loop is fully event-driven when needed.

---

## Phase 6: User Story 3 — Webhook Support (Priority: P2)

**Goal**: Optional webhook endpoint on the existing dashboard server validates HMAC-SHA256 and wakes the daemon cycle immediately on valid delivery. Invalid signatures are rejected with HTTP 401.

**Independent Test**: Enable `webhooks.enabled: true`, configure secret, send a test POST via `curl` with a valid signature. Verify HTTP 200 is returned and a cycle fires within 2 seconds.

- [X] T039 [US3] Implement `verify_github_signature(body, secret, header)` function in `src/coordinare/dashboard.py` using stdlib `hmac` + `hashlib`; constant-time compare via `hmac.compare_digest`; return `False` (not raise) on invalid/missing header
- [X] T040 [US3] Implement `register_webhook_route(app, path, secret, trigger)` function in `src/coordinare/dashboard.py`: POST route reads raw body, calls `verify_github_signature`, returns HTTP 401 with structured log on failure, sets `trigger` and returns HTTP 200 `{"status": "ok"}` on success
- [X] T041 [US3] Pass `_webhook_trigger` event from `CoordinareDaemon` instance to `register_webhook_route` in `src/coordinare/__main__.py` when `config.webhooks.enabled` is True; call `register_webhook_route` on the dashboard FastAPI app before starting uvicorn
- [X] T042 [US3] Guard webhook route registration in `src/coordinare/__main__.py` so that when `config.webhooks.enabled` is `False` no route is registered (FR-012)
- [X] T043 [P] [US3] Write unit tests for HMAC verification in `tests/unit/dashboard/test_webhook.py`: valid signature returns True; invalid signature returns False; missing header returns False; constant-time comparison used (no early return on length mismatch)
- [X] T044 [P] [US3] Write unit tests for webhook route in `tests/unit/dashboard/test_webhook.py` using FastAPI `TestClient`: valid POST returns 200 and sets trigger event; invalid signature returns 401 and does NOT set trigger; `webhooks.enabled=False` means route is not registered

**Checkpoint**: Webhook endpoint fully functional. Valid events wake the daemon cycle within 2 seconds. Invalid signatures are rejected and logged.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T045 [P] Update `CLAUDE.md` via `.specify/scripts/bash/update-agent-context.sh claude` to record `PyJWT[crypto]>=2.8` as an active dependency
- [X] T046 [P] Run `ruff check src/coordinare/auth/ src/coordinare/dashboard.py src/coordinare/daemon.py src/coordinare/config.py src/coordinare/__main__.py` and fix any lint issues
- [X] T047 Run full test suite `.venv/bin/pytest` and verify coverage does not regress below the configured threshold
- [ ] T048 Validate the quickstart.md PAT config example works end-to-end against a real GitHub project board

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 (needs auth/ skeleton from T002)
- **US2 / Phase 3**: Depends on Foundational (needs Protocol T004, config fields T005–T014)
- **US1 / Phase 4**: Depends on US2 (needs `PatAuth` and `build_auth` stub from T016–T017; `GitHubService` refactor from T018–T019)
- **US4 / Phase 5**: Depends on Foundational (needs poll_interval_seconds change T008, webhook config T009–T010); can proceed in parallel with US2
- **US3 / Phase 6**: Depends on US4 (needs `_webhook_trigger` event T033)
- **Polish (Phase 7)**: Depends on all user story phases complete

### User Story Dependencies

```
Phase 1 (Setup)
    └── Phase 2 (Foundational)
            ├── Phase 3 (US2 — PAT compat)  ──► Phase 4 (US1 — App mode)
            └── Phase 5 (US4 — Polling)     ──► Phase 6 (US3 — Webhooks)
                                                         └── Phase 7 (Polish)
```

### Within Each Phase

- Tasks marked [P] have no inter-task dependency and can run in parallel
- `_validate_auth_config` (T011) must follow T005, T006, T007 (fields must exist first)
- `AppAuth.get_token()` (T027) must follow JWT (T025) and fetch (T026)
- Daemon event loop (T034, T035) must follow `_webhook_trigger` init (T033)
- Webhook route registration (T041) must follow `register_webhook_route` impl (T040) and T033

---

## Parallel Opportunities

### Phase 2 (Foundational) — run T005–T008 together
```
T005 github_auth field   |
T006 app fields          |  all touch different lines of config.py
T007 token optional      |  (no conflicts — parallel edit is safe in agent context)
T008 poll_interval ge=0  |
```

### Phase 3 (US2) — after T016–T020 complete, run tests in parallel
```
T021 PatAuth unit tests           |
T022 GitHubService auth tests     |  different test files
```

### Phase 4 (US1) — AppAuth internals can be written bottom-up in parallel, then tests
```
T023 _load_key      |
T024 __init__       |  all in app.py but non-overlapping methods
T025 _make_jwt      |
T026 _fetch_token   |
```
```
T031 JWT unit tests    |
T032 cache/refresh     |  different test functions, same file
```

### Phase 6 (US3) — tests in parallel
```
T043 HMAC verification tests  |
T044 webhook route tests       |  both in test_webhook.py but different test functions
```

---

## Implementation Strategy

### MVP (Phase 1–3 only — PAT Backwards Compat)

1. Complete Phase 1: Setup (T001–T003)
2. Complete Phase 2: Foundational (T004–T015)
3. Complete Phase 3: US2 PAT compat (T016–T022)
4. **STOP AND VALIDATE**: Existing PAT configs work; `GitHubService` uses `GitHubAuth` protocol
5. This alone merges cleanly with no behaviour change for PAT users

### Incremental Delivery

1. Phases 1–3 → PAT compat shipped (MVP, zero user-visible change)
2. Phase 4 → GitHub App mode unlocked
3. Phase 5 → Poll disable / configurable interval
4. Phase 6 → Webhook endpoint
5. Phase 7 → Polish and final validation

### Single-Developer Sequence

P1 stories first (US2 → US1), then P2 stories (US4 → US3), then Polish. Total: 48 tasks.

---

## Notes

- [P] tasks = different files or non-overlapping regions, no completion dependencies
- Tests for each user story validate independently before moving to the next phase
- `AppAuth` tests use a synthetic RSA key pair generated in the test fixture (no real GitHub credentials required)
- `verify_github_signature` is a pure function — no FastAPI needed for HMAC tests
- The `_webhook_trigger` event passed to `CoordinareDaemon` in tests should be an `asyncio.Event()` created inside the test's event loop
