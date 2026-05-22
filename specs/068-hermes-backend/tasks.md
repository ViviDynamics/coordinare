# Tasks: Hermes Performer Backend

**Feature**: 068-hermes-backend | **Branch**: `068-hermes-backend`
**Inputs**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

Tests are explicitly required by **FR-013** and **SC-005**, so test
tasks are included alongside implementation tasks within each story
phase.

---

## Phase 1 — Setup

- [ ] T001 Confirm `hermes-agent` (and the `hermes` CLI on PATH) is bakeable into the performer container; record the version pin in `agent/performer/pyproject.toml` if a Python install is needed, otherwise note that the binary lands via the image. Verify via `which hermes && hermes --version` inside the dev container.
- [X] T002 [P] Add a placeholder test module file at `agent/performer/tests/unit/backends/test_hermes_backend.py` (empty body with module docstring) so subsequent test tasks can run in parallel without import races.

## Phase 2 — Foundational

> Blocking prerequisites for every user story.

- [ ] T003 Read and reconfirm the `BackendAdapter` Protocol in `agent/performer/src/performer/backends/base.py` and the one-shot CLI pattern in `agent/performer/src/performer/backends/junie.py` — these are the structural reference for `HermesBackend`. No code change; a documented checkpoint to prevent drift.
- [X] T004 [P] Sketch the `HermesBackend` skeleton in `agent/performer/src/performer/backends/hermes.py`: imports, class declaration with `BackendAdapter` Protocol conformance, empty `start` / `get_status` / `drain_events` / `relay_feedback` / `stop` methods, and `_finalize()` stub. Must import cleanly (`python -c "from performer.backends.hermes import HermesBackend"`).
- [X] T005 [P] Add the prompt-builder helper invocation site in `hermes.py` reusing the same shared helpers `junie.py` / `opencode.py` / `codex.py` call. Verify by importing the helper symbol; do not yet wire it into `start()`.

## Phase 3 — User Story 1 (P1): Operator selects Hermes for a performer

**Story goal**: `backend: hermes` in performer config dispatches a card to `HermesBackend`, which launches Hermes and reports a terminal status through the existing protocol.

**Independent test**: Configure a performer with `backend: hermes` for the `implementer` role, dispatch one card, observe `working → done` and a PR opened via the existing performer wrapper.

- [X] T006 [US1] Register `"hermes"` in the backend factory dict in `agent/performer/src/performer/backends/__init__.py` (entry: `"hermes": ("performer.backends.hermes", "HermesBackend")`). Match the alphabetical / existing ordering of peer entries.
- [X] T007 [P] [US1] Implement `HermesBackend.start()` in `agent/performer/src/performer/backends/hermes.py` per `contracts/backend_adapter.md`: create `tempfile.mkdtemp(prefix="hermes-job-")` as `_profile_dir`, write `hermes.config.yaml` with `disabled_toolsets: [gateway, messaging, cron, clarify, user_memory]`, validate required env vars (`HERMES_PROVIDER`, `HERMES_API_KEY`, model), build prompt from `Score` via shared helpers (persona → card → AC → clarifications → relay-feedback queue → architecture plan → role output rules), spawn `hermes chat -q "<prompt>" --quiet -z --toolsets terminal,file,search,browser,todo --provider $HERMES_PROVIDER --model <resolved> [--base-url …]` as `asyncio.subprocess.Process` with `HERMES_HOME=<_profile_dir>`, set `_status` to `working`. Strip forbidden `HERMES_GATEWAY_* / HERMES_MESSAGING_* / HERMES_CRON_* / HERMES_USER_MEMORY_*` from the subprocess env.
- [X] T008 [P] [US1] Implement `HermesBackend.get_status()` in the same file per `contracts/status_mapping.md`: non-blocking poll of `_proc.returncode`; map to `working` / `done` / `error` with the documented `reason` strings (`malformed_output`, `subprocess_exit:<code>`, `missing_env:<NAME>`). Sticky terminal.
- [X] T009 [P] [US1] Implement `HermesBackend.drain_events()` mirroring `junie.py`: drain `_events`, redact any value matching `HERMES_API_KEY` before returning.
- [X] T010 [US1] Implement `HermesBackend._finalize()` in `hermes.py`: idempotent; `shutil.rmtree(_profile_dir, ignore_errors=False)` with a structlog `info` line on outcome (success / exception); clear `_proc`. Wire it into every terminal transition in `get_status()` and `stop()`.
- [X] T011 [US1] Update `config.example.yaml` at repo root with a `backend: hermes` example performer block and an inline-commented env-var section listing `HERMES_PROVIDER`, `HERMES_API_KEY`, `HERMES_MODEL`, `HERMES_BASE_URL` (with a note that `HERMES_HOME` is adapter-managed and operator values are intentionally ignored).
- [X] T011a [US1] Add focused per-backend example configs at repo root — `config.example.hermes.yaml` (primary deliverable for this spec) plus parallel `config.example.{codex,claude_code,opencode,opencode_compat,cursor,junie}.yaml` so every supported backend has a copy-pasteable starting point. Each file shows only the wiring that backend needs (auth model, image, volumes/env, performers block).
- [X] T012 [P] [US1] Add test `test_backend_factory_registers_hermes` in `agent/performer/tests/unit/backends/test_hermes_backend.py`: import the factory, request `"hermes"`, assert it returns a `HermesBackend` instance. Also assert that an unknown backend name raises with a message that includes `"hermes"` in the supported-list.
- [X] T013 [P] [US1] Add test `test_start_spawns_hermes_cli_with_isolated_home` in the same file: monkeypatch `asyncio.create_subprocess_exec`; call `start()` with a fixture `Score`; assert the spawned argv contains `chat`, `-q`, `--quiet`, `-z`, `--toolsets terminal,file,search,browser,todo`, and that env passed in has a fresh `HERMES_HOME` under the temp dir (not `~/.hermes`).
- [X] T014 [P] [US1] Add test `test_start_fails_fast_on_missing_env` in the same file: clear `HERMES_API_KEY` from env; assert `start()` transitions to terminal `error` with `reason="missing_env:HERMES_API_KEY"` and no subprocess is spawned and the profile dir is removed (i.e., `_finalize()` ran).
- [X] T015 [P] [US1] Add test `test_persona_and_card_context_flow_into_prompt` in the same file: build a `Score` whose persona contains a distinctive marker; monkeypatch subprocess; assert the marker appears in the `-q` prompt string, and that acceptance criteria + clarifications are present in declared order.
- [X] T015a [P] [US1] Add parametrized test `test_role_parity_prompt_contains_role_block` in `agent/performer/tests/unit/backends/test_hermes_backend.py` covering every FR-001a role (`assessor`, `architect`, `implementer`, `reviewer`, `qa`, `security`, `closer`): for each role, build a `Score`, invoke the prompt builder, and assert the role-specific output-requirements block appears verbatim in the `-q` prompt passed to `hermes chat`.

**Story 1 checkpoint** — at completion: factory accepts `hermes`, an end-to-end happy path runs, the operator can switch one config line and observe `pr_opened` from the existing performer wrapper (SC-001, SC-002).

---

## Phase 4 — User Story 2 (P1): Hermes only speaks through GitHub

**Story goal**: Hermes runs are silent on operator messaging surfaces; `~/.hermes` is untouched; all human-visible artifacts appear only on GitHub.

**Independent test**: Run an end-to-end card and verify (a) no direct messages emitted, (b) `~/.hermes` listing + mtimes unchanged, (c) all human-readable output traceable to the coordinare-managed issue/PR.

- [X] T016 [US2] Harden capability gating in `HermesBackend.start()` (`agent/performer/src/performer/backends/hermes.py`): the `--toolsets` allow-list is constant `terminal,file,search,browser,todo`; the written `hermes.config.yaml` always carries the full `disabled_toolsets: [gateway, messaging, cron, clarify, user_memory]` list. Extract these as module-level constants `ALLOWED_TOOLSETS` and `DISABLED_TOOLSETS` so tests can import them.
- [X] T017 [US2] In `start()`, explicitly override `HERMES_HOME` regardless of inherited env (FR-005) and pop forbidden `HERMES_*` env vars from the subprocess env. Add a structlog `info` line logging the resolved provider/model/base_url (NOT the api key) and the absolute `HERMES_HOME` path.
- [X] T018 [P] [US2] Add test `test_disabled_toolsets_written_into_profile_config` in `agent/performer/tests/unit/backends/test_hermes_backend.py`: after `start()`, read the `hermes.config.yaml` in the profile dir and assert it contains every entry of `DISABLED_TOOLSETS`.
- [X] T019 [P] [US2] Add test `test_allowed_toolsets_passed_on_cli` in the same file: assert the spawned argv contains `--toolsets terminal,file,search,browser,todo` and contains none of `gateway` / `messaging` / `cron` / `clarify` / `user_memory` anywhere in the command line.
- [X] T020 [P] [US2] Add test `test_operator_hermes_home_is_ignored` in the same file: set the parent process `HERMES_HOME=/tmp/operator-hermes`; call `start()`; assert the spawned env's `HERMES_HOME` is NOT `/tmp/operator-hermes` and matches the temp `hermes-job-*` path.
- [X] T021 [P] [US2] Add test `test_forbidden_env_vars_stripped` in the same file: set `HERMES_GATEWAY_URL`, `HERMES_MESSAGING_TOKEN`, `HERMES_CRON_SPEC`, `HERMES_USER_MEMORY_PATH` in the parent env; assert none appear in the subprocess env passed to `asyncio.create_subprocess_exec`.
- [X] T022 [P] [US2] Add test `test_api_key_redacted_in_events` in the same file: seed an event containing the API key value; call `drain_events()`; assert the returned payload does not contain the key.

**Story 2 checkpoint** — at completion: capability + env gating is provably tight; SC-003 (zero direct messages, `~/.hermes` untouched) is verifiable via tests plus the quickstart smoke.

---

## Phase 5 — User Story 3 (P2): Hermes behaves like other coding backends

**Story goal**: Lifecycle parity — `working`/`done`/`error` only, queued relay feedback, clean stop, no Hermes-specific statuses leak. Covers FR-001a (full role parity), FR-002 (one-shot relay), FR-009 (vocabulary), FR-010 (malformed → error), FR-010a (timeout), FR-011 (cleanup on every terminal).

**Independent test**: Compare a Hermes run against an `opencode` run for the same role/card — status transitions, relay-feedback handling, and stop semantics are observably equivalent from coordinare's side.

- [X] T023 [US3] Implement `HermesBackend.relay_feedback()` in `agent/performer/src/performer/backends/hermes.py`: append to `_feedback_queue` only; no subprocess interaction; no `_status` mutation. Document inline that the next `start()`-equivalent invocation drains the queue into the prompt (R-004 / FR-002).
- [X] T024 [US3] Update the prompt-builder call in `start()` to drain `_feedback_queue` into the relay-feedback section of the prompt before clearing it; preserve insertion order.
- [X] T025 [US3] Implement `HermesBackend.stop()` in the same file per `contracts/backend_adapter.md`: set `_stop_requested`, `_proc.terminate()`, await up to 2s, `_proc.kill()` if still alive, then call `_finalize()`. Ensure `_status` ends as `error` with `reason="stopped"`. Idempotent.
- [X] T026 [US3] Ensure `get_status()` reports `error` with `reason="malformed_output"` when the Hermes output JSON file is missing/empty/unparseable on exit 0 (FR-010); no retry, no loop. Cross-check against `contracts/status_mapping.md`.
- [X] T027 [US3] Confirm in `agent/performer/src/performer/main.py` that `settings.AGENT_TIMEOUT` enforcement already calls `backend.stop()` on the Hermes adapter path (no code change expected — this task is a documented audit checkpoint). Add an inline comment in `hermes.py`'s `stop()` referencing FR-010a so future readers see the timeout contract is honoured by the shared loop, not by Hermes-specific logic.
- [X] T028 [P] [US3] Add test `test_relay_feedback_queues_without_subprocess_call` in `agent/performer/tests/unit/backends/test_hermes_backend.py`: call `relay_feedback("hi")` twice; assert no subprocess was spawned, `_status` unchanged, `_feedback_queue == ["hi", "hi"]`.
- [X] T029 [P] [US3] Add test `test_relay_feedback_drains_into_next_invocation_prompt` in the same file: queue two feedback strings, call `start()`, assert both appear in the prompt in order, and the queue is empty after `start()` returns.
- [X] T030 [P] [US3] Add test `test_stop_terminates_subprocess_and_cleans_profile` in the same file: spawn a long-running fake subprocess; call `stop()`; assert the process is terminated within 2s, `_status.state == "error"` with `reason="stopped"`, profile dir no longer exists, and a second `stop()` call does not raise.
- [X] T031 [P] [US3] Add test `test_malformed_output_resolves_to_error_not_loop` in the same file: fake subprocess exits 0 but writes an empty output file; one `get_status()` call returns `error` with `reason="malformed_output"`; profile dir is cleaned; no further subprocess spawns occur.
- [X] T032 [P] [US3] Add test `test_profile_cleanup_on_every_terminal_outcome` in the same file: parametrize over `done`, `error(malformed)`, `error(subprocess_exit)`, `error(stopped)`; in every case assert the `hermes-job-*` directory is absent after the terminal `get_status()` (or `stop()`).
- [X] T033 [P] [US3] Add test `test_no_hermes_internal_state_leaks` in the same file: even when `_events` carries Hermes-internal strings, assert `BackendStatus.state` is always one of `{working, done, error}` and any Hermes-internal mention in `reason` is prefixed with `hermes:`.

**Story 3 checkpoint** — at completion: lifecycle parity proved; SC-004 (concurrent isolation) and SC-005 (terminal error on failure injection) verified.

---

## Phase 6 — Polish & Cross-Cutting

- [X] T034 Run `.venv/bin/ruff check agent/performer/src/performer/backends/hermes.py agent/performer/tests/unit/backends/test_hermes_backend.py` and fix any findings; matching style with peer adapters.
- [X] T035 [P] Run `.venv/bin/pytest agent/performer/tests/unit/backends/test_hermes_backend.py -q` and confirm all tests pass deterministically (run twice locally to rule out flakiness).
- [X] T036 [P] Run the full performer suite `.venv/bin/pytest agent/performer/tests -q` to confirm no peer-backend regressions (FR-007 / SC-006: zero coordinare orchestration changes).
- [X] T037 [P] Verify SC-006 by running `git diff origin/main -- src/coordinare/` and confirming the diff is empty.
- [ ] T038 Execute the `quickstart.md` smoke path end-to-end (assessor → architect → implementer + failure-injection + concurrency) against a sandbox repo, capturing the pre/post `~/.hermes` listing comparison and the `/tmp/hermes-job-*` cleanup check. Attach results to the PR description.
- [ ] T038a [P] SC-007 measurement: during the T038 smoke run, instrument the performer (or wrap with a timing harness) to record the wall-clock interval from `start()` invocation to the first `working` status for one `junie`-backed implementer card and one `hermes`-backed implementer card on the same host. Report `delta_ms = hermes - junie` in the PR description and assert it is ≤200ms.

---

## Phase 7 — Cross-Backend Persona Routing (FR-015..FR-018)

Route `score.persona_instructions` to each backend's canonical, job-isolated identity slot where one exists, and drop the duplicate `## Role Instructions` block from the prompt body. Backends without a job-isolated slot (Junie, opencode, opencode_compat, cursor) keep the existing prompt-body wiring — see FR-018 for the rationale (workspace-resident persona files would risk landing in commits).

- [x] T039 [P7] In `agent/performer/src/performer/backends/hermes.py`, write `score.persona_instructions` to `<HERMES_HOME>/SOUL.md` during `start()` (only when the field is non-empty). Remove the persona block at hermes.py:537-538 so the persona is not duplicated in the prompt body. Reference FR-015 in a brief inline comment at the `SOUL.md` write site.
- [x] T040 [P7] In `agent/performer/src/performer/backends/claude_code.py`, append `--append-system-prompt <score.persona_instructions>` to the argv built around `claude_code.py:151-163` (only when the field is non-empty). Remove the persona block at claude_code.py:301-302. Reference FR-016 inline.
- [x] T041 [P7] In `agent/performer/src/performer/backends/codex.py`, remove the duplicate persona block at codex.py:590-591 (the thread-level `developerInstructions` wiring at codex.py:240 already covers FR-017). Reference FR-017 inline at the `developerInstructions` assignment so future readers see the prompt-body block was deliberately not added back.
- [x] T042 [P] [P7] In `agent/performer/tests/unit/backends/test_hermes_backend.py`, add `test_persona_written_to_soul_md_and_not_in_prompt`: set `score.persona_instructions = "you are the architect"`; call `start()`; assert (a) `<HERMES_HOME>/SOUL.md` exists with exactly that text, (b) the persisted/captured prompt body contains no `## Role Instructions` heading and no `score.persona_instructions` substring. Also add `test_no_soul_md_written_when_persona_empty`: with `persona_instructions=""`, assert `SOUL.md` does NOT exist after `start()`.
- [x] T043 [P] [P7] In `agent/performer/tests/unit/backends/test_claude_code_backend.py` (create or extend), add `test_persona_passed_via_append_system_prompt_flag`: assert `--append-system-prompt` followed by the exact persona string appears in the spawned argv, and the prompt body (`-p` argument) contains no `## Role Instructions` heading. Add `test_append_system_prompt_omitted_when_persona_empty`.
- [x] T044 [P] [P7] In `agent/performer/tests/unit/backends/test_codex_backend.py`, add `test_persona_only_in_developer_instructions_not_in_prompt`: assert `developerInstructions` carries the persona string AND the constructed prompt body contains no `## Role Instructions` heading.
- [x] T045 [P] [P7] Regression guard for FR-018: add `test_persona_kept_in_prompt_for_workspace_backends` in the relevant existing tests for `junie.py`, `opencode.py`, `opencode_compat.py`, and the cursor adapter — assert the prompt body still contains the `## Role Instructions` block. This locks in the intentional split so a future refactor can't silently move persona out for these backends.
- [x] T046 [P7] Update `config.example.hermes.yaml` to add a one-line note under the `# --- Containerized Performer (Hermes) ---` block explaining that `persona_instructions:` from `performers.<role>.persona_instructions` is written to `$HERMES_HOME/SOUL.md` per job (not into the prompt body). No new config keys.
- [x] T047 [P7] Run `.venv/bin/ruff check agent/performer/src/performer/backends/{hermes,claude_code,codex}.py agent/performer/tests/unit/backends/` and `.venv/bin/pytest agent/performer/tests -q`. Both must pass before Phase 7 is considered complete.

---

## Dependencies

```text
T001, T002          (Setup, parallel)
   │
   ▼
T003 → T004, T005   (Foundational; T004/T005 parallel after T003)
   │
   ▼
US1: T006 → T007 → T008, T009 (parallel) → T010 → T011 → T012, T013, T014, T015 (parallel)
   │
   ▼
US2: T016 → T017 → T018, T019, T020, T021, T022 (parallel)
   │
   ▼
US3: T023 → T024 → T025 → T026 → T027 → T028, T029, T030, T031, T032, T033 (parallel)
   │
   ▼
Polish: T034 → T035, T036, T037 (parallel) → T038
```

**Story-level dependency**: US2 and US3 both extend the `HermesBackend` started in US1; they MUST complete in order. Within each story, the parallel `[P]` test tasks can be authored concurrently once their implementation predecessors land.

## Parallel Execution Examples

- After T006 (factory registration) lands, T007/T008/T009 can be drafted in parallel by separate sub-agents — they touch the same file (`hermes.py`) but distinct methods, so coordinate via a single PR or pre-agreed insertion points.
- T012–T015 (US1 tests) are all in the same test file but cover disjoint test functions; parallel authoring is safe with a final lint pass.
- T018–T022 (US2 tests) are mutually independent.
- T028–T033 (US3 tests) are mutually independent.
- Polish phase: T035 / T036 / T037 run concurrently (separate processes).

## Implementation Strategy

- **MVP** = US1 only. With US1 complete, an operator can switch `backend: hermes` for one role and observe a PR open through the existing wrapper (SC-001, SC-002). The capability gating (US2) and lifecycle parity (US3) harden it for production but are not required to demonstrate the feature surface.
- **Increment 1 (MVP)**: US1 — ship as a feature-flagged adapter; smoke-test on assessor only.
- **Increment 2**: US2 — capability gating hardened, env stripping in place; smoke-test confirms `~/.hermes` untouched.
- **Increment 3**: US3 — relay-feedback queue, clean stop, malformed-output handling, full role parity; run concurrency smoke.

## Format Validation

All 38 tasks follow the strict checklist format `- [ ] TID [P?] [US?] description with file path`. Setup (T001–T002), Foundational (T003–T005), and Polish (T034–T038) tasks intentionally carry no `[US]` label; every Phase 3–5 task carries the correct `[US1]` / `[US2]` / `[US3]` label per the user-story mapping in spec.md.
