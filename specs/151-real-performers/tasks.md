---
description: "Task list for Spec 151 — Board-Simulation Benchmark: Real Performers"
---

# Tasks: Board-Simulation Benchmark — Real Performers (Spec 151)

**Input**: Design documents from `/specs/151-real-performers/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — the plan's Constitution Check (II) mandates a performer-boundary
contract test and no regression of 134's suite (SC-006). Deterministic unit/contract
tests are free; the single real-model end-to-end run is an **opt-in, skipped-by-default**
lane (needs Docker + a cheap model endpoint).

**Organization**: Tasks are grouped by user story. US1 (the real run) consumes the fake
boundary built in US2 — the two P1 stories together are the MVP.

## The end-to-end dispatch path (grounded — governs several tasks)

`config.github_api_url` → `dispatch_performer.py:1395` sets `card_context["github_api_url"]`
→ `http_performer_service._build_job_payload:860` stashes **all** of `card_context` into
`JobInitPayload.metadata` (the payload is `extra="ignore"`; there is **no** typed
`github_api_url` field and **no** container env var) → the performer reads
`Score.github_api_url` and applies it at runtime in `main.py:1470-1494`, which **only
accepts `https://<any>` or `http://{localhost,127.0.0.1,::1}`** and otherwise falls back
to `https://api.github.com` (a real-GitHub leak). Therefore:

- `github_graphql_url` must ride the **same** path: a new `Score.github_graphql_url` field
  + a parallel `main.py` override (T007), not an env var or payload field.
- The fake host must be seen by the performer as `http://127.0.0.1:<port>` → the bench
  performer container runs with **`--network host`** (Linux) so loopback reaches the
  host's fake services (T017). `performer_lifecycle.start_ephemeral` has no network-mode
  support today — that is the one real launch-path addition.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1–US4 from spec.md
- Every task lists an exact file path.

## Path Conventions

Single project: coordinare at `src/coordinare/`, performer package at
`agent/performer/src/performer/`, tests at `tests/`, CLI at `scripts/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Minimal scaffolding for the opt-in real lane. 134's `bench/` package,
fixtures, and `FakeGitHubService` already exist and are reused.

- [X] T001 [P] Add a real-mode sample config `specs/151-real-performers/examples/bench-real.yaml` (single symphony, a cheap model endpoint, reviewer/security/qa gate stages enabled, ephemeral Docker transport with `--network host`) documenting the opt-in run from quickstart.md.
- [X] T002 [P] Register a `real` pytest marker (skipped unless `RUN_REAL_BENCH` is set) in `pyproject.toml`, with a comment recording that the skip is deliberate and tracked by this spec (opt-in paid/Docker lane; SC-006 keeps the free deterministic lane green) — the tracked justification for Constitution II "no untracked skips".

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The "Git-host configuration" injection points (data-model.md). Each defaults
to today's production behavior; a run is "real bench mode" only when the harness sets them.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 [P] Add `git_base_url: str = "https://github.com"` config field + normalization to `src/coordinare/config.py` (mirror `github_api_url` at `config.py:794` and its validator at `:1239`).
- [X] T004 Build `repo_url`/`clone_url` from `config.git_base_url` in `src/coordinare/workspace.py` (replace the two hardcodes at `workspace.py:263` and `:284`) and thread `git_base_url` into `WorkspaceManager.__init__`. (depends on T003)
- [X] T005 [P] Add `GITHUB_GRAPHQL_URL: str = "https://api.github.com/graphql"` setting to `agent/performer/src/performer/config.py` (next to `GITHUB_API_URL` at `config.py:35`).
- [X] T006 [P] Coordinare side: set `card_context["github_graphql_url"] = config.github_graphql_url` in `src/coordinare/graph/nodes/dispatch_performer.py` (parallel to `github_api_url` at `:1394-1395`). It rides into `JobInitPayload.metadata` automatically (`http_performer_service.py:860` stashes all of `card_context`; the payload is `extra="ignore"`) — **no** payload-model or container-env change.
- [X] T007 Performer bridge: add `github_graphql_url: str = ""` to `Score` in `agent/performer/src/performer/models.py` (next to `github_api_url` at `:130`) and apply it at runtime in `agent/performer/src/performer/main.py` — mirror the `github_api_url` override block (`main.py:1470-1494`), same whitespace/credentials/query and **https-any / http-localhost-only** validation, setting `settings.GITHUB_GRAPHQL_URL`. (depends on T005; **same file `models.py` as T009 → sequential**)
- [X] T008 Use `get_settings().GITHUB_GRAPHQL_URL` in `resolve_pr_review_threads` in `agent/performer/src/performer/github.py` (replace hardcode at `:399`; update the two POSTs at `:422` and `:465`). (depends on T005)
- [X] T009 Relax the `repo_url` validator in `agent/performer/src/performer/models.py` (`:68`, `:161-167`) to also accept a `git://` URL **only when** `ALLOW_INSECURE_REPO_URL` is set in the environment; default keeps `https`-only. (**same file as T007 → sequential**)

**Checkpoint**: The coordinare can point git + REST + graphql at a loopback host, the performer applies both URL overrides (http accepted only for loopback), and accepts a `git://` remote when the bench flag is set — production behavior unchanged.

---

## Phase 3: User Story 2 - Performer's GitHub calls served by the fake, never real GitHub (Priority: P1)

**Goal**: Stand up the harness-local fake boundary — a `git daemon` remote and an aiohttp
REST/graphql surface — both backed by `FakeGitHubService`, so a performer clones/pushes and
opens/polls its PR without touching real GitHub.

**Independent Test**: Drive the real performer `github.py` client and a `git clone`/push
against the running fake and confirm every call is served locally in the expected shapes;
assert the dispatch emits loopback URLs and nothing targets `api.github.com`/`github.com`.

### Tests for User Story 2 ⚠️ (write first, ensure they fail)

- [X] T010 [P] [US2] Contract test `tests/contract/test_151_performer_boundary.py`: run the performer's real `github.py` calls (`create_pull_request`, `get_default_branch`, `get_check_runs`, `get_pr_head_sha`, `get_existing_pull_request`) against a live `FakeGitHubServer`; assert responses match `contracts/performer-facing-rest.md`.
- [X] T011 [P] [US2] Unit test `tests/unit/test_151_fake_rest.py`: handlers return performer-parseable JSON; the 422 "already exists" idempotency path; `/graphql` best-effort (non-2xx tolerated → `resolve_pr_review_threads` returns 0); check-runs derived from the fake's real pytest; **PR-create returns 5xx when `open_pr` fails, surfacing a `GitHubAPIError`-shaped body** (edge case → G1).
- [X] T012 [P] [US2] Unit test `tests/unit/test_151_dispatch_injection.py`: `dispatch_performer` emits `card_context["github_api_url"]` **and** `["github_graphql_url"]`, both survive into `JobInitPayload.metadata`, and `WorkspaceManager` builds `repo_url` from `config.git_base_url` (bench → `git://127.0.0.1:<git_port>/…`; default → `https://github.com/…`) — the coordinare→performer dispatch interface carries the fake host (C2).
- [X] T013 [P] [US2] Unit test `tests/unit/test_151_performer_config.py`: `Score.github_api_url` **and** `Score.github_graphql_url` overrides are applied by `main.py` — `http://127.0.0.1:*` accepted, `http://<non-loopback>` **rejected** (falls back, no leak), `https://*` accepted; `repo_url` admits `git://` only when `ALLOW_INSECURE_REPO_URL` is set, `https`-only otherwise.

### Implementation for User Story 2

- [X] T014 [US2] Create `src/coordinare/bench/fake_github_server.py`: an aiohttp app implementing the 5 REST endpoints + `POST /graphql`, delegating every read/write to an injected `FakeGitHubService` (no local PR/CI state) per `contracts/performer-facing-rest.md`.
- [X] T015 [US2] Add `git daemon` lifecycle to `fake_github_server.py`: start a subprocess serving the bare repo with `--export-all --enable=upload-pack --enable=receive-pack --port=<git_port>` per `contracts/git-remote.md`. Bind **9418** by default; if it is unavailable, pick a free port (bind-test a socket) and thread the actual `<git_port>` into `config.git_base_url` (`git://127.0.0.1:<git_port>/bench-repo`) so a stale daemon or another local `git://` service can't collide (M2).
- [X] T016 [US2] Add a pre-dispatch reachability probe (fail fast with an actionable error, no hang — **including a clear message when the git daemon or REST server cannot bind its port**) and a teardown-safe `stop()` (kill daemon, close server; never raises) to `fake_github_server.py` (research D6/D9).
- [X] T017 [US2] Networking (**revised in review — `network_mode` was REMOVED; see T017a**): add `network_mode` (e.g. `host`) support to `performer_lifecycle.start_ephemeral` in `src/coordinare/services/performer_lifecycle.py` (emit `--network <mode>` in the `docker run` args after line 131) and inject `ALLOW_INSECURE_REPO_URL=1` via `config.env`; the bench sets `network_mode="host"` and loopback URLs (`http://127.0.0.1:<port>`, `git://127.0.0.1:9418`) so the performer's http-localhost validation (`main.py:1489`) accepts them. (resolves U1 + the loopback constraint)

**Checkpoint**: The fake boundary serves clone/push + PR/CI/graphql over loopback; no configured URL or dispatch field points at real GitHub.

---

## Phase 4: User Story 1 - Drive a card through the full lifecycle with real performers (Priority: P1) 🎯 MVP

**Goal**: Run `run_board(stub=False)` — real model dispatch through `/jobs`, real code +
real PR + real CI — to a terminal state, emitting 134's artifact now recording real
per-persona dispatches. (Consumes the US2 fake boundary.)

**Independent Test**: Run one cheap fixture in real mode; the card traverses the real
personas to a terminal state (merged for a satisfiable fixture) and the artifact validates
with >1 real dispatch recorded.

### Tests for User Story 1 ⚠️

- [X] T018 [P] [US1] `tests/integration/test_151_real_e2e.py` (marked `real`, skipped without `RUN_REAL_BENCH`): one cheap fixture, real mode → card reaches terminal (SC-001); artifact validates with >1 `PersonaDispatch` carrying `model`/`backend`/`tokens_processed`, distinguishable from the stub's single entry (SC-002); with GitHub egress blocked the run still terminates + emits an artifact (SC-003, SC-005); **when the fake fails PR-create the card reaches a terminal non-merge state and an artifact is still emitted** (edge case → G1).
- [X] T031 [P] [US1] **Deterministic** (free, no model) unit test `tests/unit/test_151_runner_failure.py`: drive the real runner path (`run_board(stub=False)`) with an injected `FakeGitHubServer`/dispatch that raises (PR-create 5xx **and** an unreachable-server case) and assert the teardown-`finally` (T021) still runs — server stopped, ephemeral containers/scratch cleaned — **and** a schema-valid artifact with a terminal non-merge outcome is emitted (FR-008/FR-009/FR-010). Closes the gap where these resilience guarantees were exercised only by the opt-in paid lane (T018). (numbered T031 to keep existing IDs stable; depends on T019/T021.)

### Implementation for User Story 1

- [X] T019 [US1] Add the real path to `src/coordinare/bench/runner.py`: when `stub=False`, start the `FakeGitHubServer`, set `config.git_base_url`/`github_api_url`/`github_graphql_url` to the loopback bench host, dispatch performers BRIDGED with `extra_hosts: [host.docker.internal:host-gateway]` + `ALLOW_INSECURE_REPO_URL=1` + `ALLOW_HOST_GATEWAY_GITHUB=1` (**revised in review**: `network_mode="host"` was removed), set the full `lifecycle_sequence` (assessor→architect→implementer→reviewer→security→qa→tech_writer→closer), and build the graph with **no** node overrides.
- [X] T020 [US1] Record each real per-persona dispatch (stage, role, model, backend, status, started/finished, best-effort `tokens_processed` from `BackendEventType.cost`) into 134's `dispatch_log` **and** populate the `model`/`backend`/`tokens_processed` fields (already present in the schema, `artifact.py:53-63`) when constructing `PersonaDispatch` in `_build_artifact` (`runner.py:252-258`) via `src/coordinare/bench/recording_performer.py` (research D8; L2).
- [X] T021 [US1] Wrap the real run in a teardown-safe `finally` in `runner.py`: stop the server, remove ephemeral performer containers, `rmtree` scratch, and always emit a schema-valid artifact even on failure (FR-008/FR-009, research D9). (touches `runner.py` — sequential after T019)
- [X] T022 [P] [US1] Add a `--real` flag to `scripts/board_bench.py` invoking `run_board(stub=False)`.

**Checkpoint**: A cheap real run drives a card to a terminal state and writes an artifact with real dispatches — the MVP.

---

## Phase 5: User Story 3 - One consistent PR identity end-to-end (Priority: P2)

**Goal**: The PR the performer opens via the fake REST is the same `PR_N`
`FakeGitHubService` merges — one identity from `open_pr` to `squash_merge`.

**Independent Test**: Capture the `pr_node_id` the performer minted and assert the
coordinare's later `check_mergeability`/`squash_merge` act on that same record.

### Tests for User Story 3 ⚠️

- [X] T023 [P] [US3] Unit test `tests/unit/test_151_identity.py`: `POST /pulls` for a pushed branch returns `node_id` equal to `FakeGitHubService.open_pr(...)`'s `pr_id`, and `check_mergeability`/`squash_merge` on that id resolve to the same PR and produce a real local merge (SC-004).

### Implementation for User Story 3

- [X] T024 [US3] In the `POST /repos/{o}/{r}/pulls` handler in `src/coordinare/bench/fake_github_server.py`, resolve the incoming `head` branch → seeded `issue_item_id` via a runner-supplied `head_ref→card_id` index and call `FakeGitHubService.open_pr(issue_item_id, head_ref, base_ref)`; return that record's `url` as `html_url` and `pr_id` as `node_id` (research D3).
- [X] T025 [US3] Pass the `head_ref→card_id` branch index (derived from `fixtures_by_card`, `runner.py:152`) into the `FakeGitHubServer` at start in `src/coordinare/bench/runner.py`. (touches `runner.py` — sequential w.r.t. Phase 4 runner tasks)

**Checkpoint**: Opened PR and merged PR are provably the same record.

---

## Phase 6: User Story 4 - The approver keys on real gate passes (Priority: P2)

**Goal**: In a real run the card reaches review only after the reviewer/security/qa gates
actually pass; the `gates_green` approver then approves on green CI, and withholds on red.

**Independent Test**: A real run with green CI + passing gates → approver approves; one with
red CI (or a gate not passed) → approver withholds and the card does not merge.

### Tests for User Story 4 ⚠️

- [X] T026 [P] [US4] Unit test `tests/unit/test_151_approver_real.py`: with the full gate-bearing lifecycle, `card_status` reaches `IN_REVIEW` **only after** gates pass — assert that a failed gate keeps the card out of `IN_REVIEW`; `gates_green` (`src/coordinare/bench/approver.py`) approves on `ci_green + IN_REVIEW` and withholds when CI is red (no APPROVED review, `check_mergeability.mergeable == False`). (I1)

### Implementation for User Story 4

- [X] T027 [US4] Ensure the real-mode config `specs/151-real-performers/examples/bench-real.yaml` **enables the reviewer/security/qa gate stages** so `IN_REVIEW` is reached only after real gate passes — this is what makes `gates_green`'s `card_status == "IN_REVIEW"` proxy valid in real mode, and it is coupled to T019's full `lifecycle_sequence` (the sequence runs the personas; the config enables the gates). Document the coupling in quickstart.md. (I1)

**Checkpoint**: Approval in real runs is driven by real gate outcomes + green CI, not the stub proxy.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T028 [P] Validate `specs/151-real-performers/quickstart.md` end-to-end (run the documented `--real`, `--network host`, and egress-blocked commands) and correct any drift.
- [X] T029 Run 134's full suite (`make test-all`) and confirm no regression and the stub path is unchanged (SC-006).
- [X] T030 [P] Confirm and document that `ALLOW_INSECURE_REPO_URL` + `git://` + `extra_hosts`/`ALLOW_HOST_GATEWAY_GITHUB` are bench-only with production defaults untouched (Constitution I) — a note in `research.md` D5 and the performer `models.py` docstring.
- [X] T017a [REVIEW] Remove the `network_mode` knob entirely (model field, `start_ephemeral` branch, port guard, tests). It was dead config — no YAML in the repo ever set it, both bench configs use bridge + `extra_hosts` — and it carried three defects: host-netns iptables wiping the HOST's OUTPUT chain when combined with `egress_allowlist`, a hardcoded `127.0.0.1:8088` endpoint that collides across concurrent performers, and non-functioning host reachability on Docker Desktop. Regression test: `test_start_ephemeral_never_uses_host_networking`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: after Setup — BLOCKS all stories. T004→T003; T007→T005; T008→T005; T007 and T009 both edit `models.py` (sequential).
- **US2 (Phase 3)**: after Foundational. The fake boundary the other stories consume.
- **US1 (Phase 4)**: after **US2** (needs the fake server) + Foundational. The MVP — not independently runnable without US2's boundary.
- **US3 (Phase 5)**: after US2 (extends the PR-create handler).
- **US4 (Phase 6)**: after US1 (needs the full gate-bearing lifecycle running); T027 is coupled to T019.
- **Polish (Phase 7)**: after all desired stories.

### Shared-file hotspots (must be sequential, NOT [P])

- `agent/performer/src/performer/models.py`: T007 → T009.
- `src/coordinare/bench/fake_github_server.py`: T014 → T015 → T016 → T024.
- `src/coordinare/bench/runner.py`: T019 → T021 → T025.

### Parallel Opportunities

- Setup: T001, T002 in parallel.
- Foundational: T003, T005, T006 in parallel (T004 after T003; T007/T008 after T005; T009 after T007).
- US2 tests: T010–T013 in parallel (four different files).
- US1: T022 (CLI) parallel with runner work; T018 parallel (test file); T031 (deterministic runner-failure test) parallel once T019/T021 land.
- Cross-story: US3 and US4 test files (T023, T026) can be written in parallel.

---

## Parallel Example: User Story 2 tests

```bash
Task: "Contract test performer boundary in tests/contract/test_151_performer_boundary.py"
Task: "Unit test fake REST handlers in tests/unit/test_151_fake_rest.py"
Task: "Unit test dispatch injection in tests/unit/test_151_dispatch_injection.py"
Task: "Unit test performer config/override in tests/unit/test_151_performer_config.py"
```

---

## Implementation Strategy

### MVP (US1 + US2 together)

1. Phase 1 Setup → Phase 2 Foundational (the injection points + the graphql `Score` bridge).
2. Phase 3 US2 — build and test the fake boundary + `--network host` launch (deterministic, free tests).
3. Phase 4 US1 — the real runner path + CLI; run one cheap fixture end-to-end.
4. **STOP and VALIDATE**: card reaches terminal, artifact has >1 real dispatch, egress-blocked run still completes, PR-create-failure still yields a terminal artifact.

Because US1 dispatches real performers *through* US2's boundary, the MVP is the US1+US2
pair — not US1 alone. US2's cheap unit/contract tests give it independent verification
without the paid model run.

### Incremental Delivery

1. Foundational + US2 → fake boundary verified (free tests).
2. + US1 → real run reaches terminal + artifact (MVP; opt-in paid run).
3. + US3 → shared PR identity asserted.
4. + US4 → approval keys on real gates.

### Notes

- The single real-model run (T018) is the only paid/Docker task; everything else is free
  and deterministic. Keep it in the opt-in `real` lane (T002).
- Every foundational injection point defaults to production behavior — commit them behind
  their defaults so `main` is never changed by their presence, only by the bench opting in.
- The performer accepts an `http` GitHub URL **only** for loopback (`main.py:1489`); the
  bench therefore requires `--network host` + `http://127.0.0.1` URLs (T017) — a
  non-loopback http host would silently fall back to real GitHub.
- 134's stub path and its suite must stay green throughout (SC-006, T029).
</content>
