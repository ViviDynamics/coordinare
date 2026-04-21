# Tasks: Performer Environment Isolation

**Input**: Design documents from `/specs/051-performer-env-isolation/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, quickstart.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: New config models shared by both user stories

- [x] T001 Add `BotIdentityConfig(BaseModel)` with `name: str = "Coordinare Bot"` and `email: str = "coordinare@localhost"` to `src/coordinare/config.py`
- [x] T002 Add `bot_identity: BotIdentityConfig = Field(default_factory=BotIdentityConfig)` and `env_passthrough: list[str] = Field(default_factory=list)` to `ProjectConfiguration` in `src/coordinare/config.py`
- [x] T003 Add `self._config = config` to `WorkspaceManager.__init__` in `src/coordinare/workspace.py` so the manager can access bot_identity and env_passthrough

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The minimal env helper used by both workspace git ops and subprocess transport

- [x] T004 Add `_build_minimal_env(config: Any) -> dict[str, str]` module-level helper in `src/coordinare/workspace.py` — copies only `PATH`, `HOME`, `TMPDIR`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `LC_CTYPE` from `os.environ`; sets `GIT_TERMINAL_PROMPT=0`; adds `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` from `config.bot_identity` (fallback defaults when config is None); copies any vars named in `config.env_passthrough` from `os.environ`
- [x] T005 Write unit tests for `_build_minimal_env` in `tests/unit/test_workspace.py` — four scenarios: (a) no config → uses defaults `"Coordinare Bot"` / `"coordinare@localhost"`, (b) config with custom identity → uses custom name/email, (c) env_passthrough copies named var when present on host, (d) env_passthrough skips named var when absent on host

**Checkpoint**: `_build_minimal_env` tested and returns correct env dict in all scenarios.

---

## Phase 3: User Story 1 — Performer uses App token and bot identity (Priority: P1)

**Goal**: Workspace git ops and performer subprocess both use minimal env with bot identity and no host credential leakage.

**Independent Test**: Dispatch a card; inspect git log on the created branch — commits show `"Coordinare Bot <coordinare@localhost>"` (or configured identity), not the host user.

- [x] T006 [US1] Replace `env = dict(os.environ)` in `WorkspaceManager._make_git_env()` in `src/coordinare/workspace.py` with `env = _build_minimal_env(self._config)` — removes host env inheritance for all workspace git subprocess calls
- [x] T007 [US1] Add `_build_subprocess_env(config: Any, github_token: str | None) -> dict[str, str]` method (or module helper) to `src/coordinare/transport/subprocess_transport.py` — calls `_build_minimal_env`-equivalent logic; additionally injects `GITHUB_TOKEN` from the explicit token arg only (never from the host env; App-mode tokens arrive via the dispatch payload); logs `subprocess_transport.env_constructed` at DEBUG with sorted var names (not values)
- [x] T008 [US1] Pass `env=self._build_subprocess_env(...)` to `asyncio.create_subprocess_exec` in `SubprocessTransport._start()` in `src/coordinare/transport/subprocess_transport.py` — SubprocessTransport constructor must accept and store `config` and `github_token` references; update construction in `src/coordinare/__main__.py` to pass these
- [x] T009 [US1] Write unit tests for subprocess env isolation in `tests/unit/test_subprocess_transport.py` — three scenarios: (a) subprocess env does NOT contain an arbitrary host var (e.g. `AWS_SECRET_ACCESS_KEY`), (b) subprocess env DOES contain `GITHUB_TOKEN` set from the provided token, (c) subprocess env DOES contain `GIT_AUTHOR_NAME` from bot_identity config

**Checkpoint**: `bin/build` passes; commits on performer branches attributed to bot identity; no host env vars in subprocess.

---

## Phase 4: User Story 2 — Configurable bot identity (Priority: P2)

**Goal**: `bot_identity.name` and `bot_identity.email` in `config.yaml` control git commit identity; sensible defaults preserved.

**Independent Test**: Set `bot_identity.name: my-bot` + `bot_identity.email: bot@company.com` in config; dispatch card; git log shows `my-bot <bot@company.com>`.

- [x] T010 [US2] Write unit tests for `BotIdentityConfig` and `ProjectConfiguration.bot_identity` / `env_passthrough` in `tests/unit/test_config.py` — four scenarios: (a) default `bot_identity.name == "Coordinare Bot"`, (b) default `bot_identity.email == "coordinare@localhost"`, (c) YAML with custom identity parses correctly, (d) `env_passthrough: [ANTHROPIC_API_KEY]` parses as list

**Checkpoint**: Config parses correctly from YAML and env vars; defaults match spec.

---

## Phase 5: Polish & Cross-Cutting Concerns

- [x] T011 Run `.venv/bin/ruff check src/coordinare/config.py src/coordinare/workspace.py src/coordinare/transport/subprocess_transport.py src/coordinare/__main__.py` — lint clean
- [x] T012 Run `.venv/bin/pytest tests/unit/test_workspace.py tests/unit/test_config.py tests/unit/test_subprocess_transport.py -q` — all new tests pass
- [x] T013 Run `bin/build` — full suite passes, coverage ≥ 90%

---

## Dependencies & Execution Order

- **Phase 1 (Setup)**: No dependencies — T001/T002 (config) and T003 (workspace) touch different files, can run in parallel
- **Phase 2 (Foundational)**: Depends on Phase 1 — `_build_minimal_env` uses `BotIdentityConfig`
- **Phase 3 (US1)**: Depends on Phase 2 — workspace + transport use the minimal env helper
- **Phase 4 (US2)**: Depends on Phase 1 only — config tests independent of US1 runtime changes
- **Phase 5 (Polish)**: Depends on all prior phases

## Implementation Strategy

1. T001–T003: Add config models + store config ref in WorkspaceManager
2. T004–T005: Build + test `_build_minimal_env`
3. T006: Swap workspace `_make_git_env` to use minimal env
4. T007–T008: Add `_build_subprocess_env` + wire into `SubprocessTransport._start()`; update `__main__.py`
5. T009: Unit tests for subprocess isolation
6. T010: Config unit tests
7. T011–T013: Lint + full build

## Notes

- `WorkspaceManager` already stores `_github_org`, `_project_name`, `_github_token`, `_auth`, `_workspace_root` — just add `self._config = config`
- `SubprocessTransport.__init__` currently takes only `executable: str, timeout: int` — add `config` and `github_token` as optional kwargs with `None` defaults for backward compatibility
- The `_build_minimal_env` helper in `workspace.py` is not reused in `subprocess_transport.py` directly (different modules) — the transport implements equivalent logic inline or in its own method
- `env_passthrough` default is `[]` — when empty, only the minimal vars are present (backward-compatible)
- `GIT_TERMINAL_PROMPT=0` must stay in both envs to prevent interactive credential prompts
