---
description: "Task list for 124 — Living Project Wiki via the Documenter"
---

# Tasks: Living Project Wiki via the Documenter

**Input**: Design documents from `/specs/124-openwiki-documenter/`
**Prerequisites**: spec.md (source of truth), contracts/

> **⚠️ PIVOTED (2026-07).** This spec originally proposed adopting LangChain's
> **OpenWiki** CLI as a documenter backend. A proof-of-concept (see
> `poc-report.md` + `benchmark-results.md`) showed OpenWiki's DeepAgents
> tool-calling was unreliable across every self-hosted model, and cloud models
> are off-limits for this role. **The OpenWiki backend was dropped.** The shipped
> design instead **enhances our own `tech_writer` documenter** to maintain a
> living `docs/wiki/` via the reliable `{files}` JSON contract. The task list
> below reflects the pivoted scope; the original OpenWiki-backend / benchmark /
> default-switch tasks (old T001–T003, T006–T018, T028–T030) are **removed** and
> retained only in git history.

**Tests**: INCLUDED — test-first per the constitution.

## Path Conventions
Existing coordinare monorepo: orchestration under `src/coordinare/…`; performer under `agent/performer/…`; coordinare tests under `tests/unit/…`; performer tests under `agent/performer/tests/…`. The documenter behavior lives in the committed `tech_writer` persona (`src/coordinare/services/persona_service.py`) — **no `config.yaml` change** (config is operator-local and git-ignored).

---

## Phase 1: Foundational — `doc_mode` field

- [X] T004 Unit test for `Score.doc_mode` (defaults `"update"`, accepts `"init"`, rejects others, old payloads validate) — `agent/performer/tests/unit/test_score_doc_mode.py`.
- [X] T005 Add `doc_mode: Literal["init","update"] = "update"` to `Score` in `agent/performer/src/performer/models.py`.

---

## Phase 2: User Story 1 — the documenter maintains the living wiki (P1) 🎯 MVP

**Goal**: The `tech_writer` documenter maintains `docs/wiki/` (README entrypoint + section pages) + `AGENTS.md`/`CLAUDE.md` pointers, using the reliable `{files}` contract on a self-hosted model. It runs every card (never skipped) and no-ops when there's nothing to record.

- [X] T101 [US1] Enhance the `tech_writer` `DEFAULT_INSTRUCTIONS` persona in `src/coordinare/services/persona_service.py`: maintain `docs/wiki/` as a source-grounded record of truth (README entrypoint linking all sections; one canonical home per topic; no invented facts; no thin stubs); add/refresh a `## Project Wiki` pointer section in `AGENTS.md`/`CLAUDE.md`; output the unchanged `{"files":[…]}` contract.
- [X] T102 [US1] Remove the spec-123 docs-path skip (and the `_should_skip_documenting` / `_documenter_backend` / `_fetch_changed_files` helpers) from `src/coordinare/graph/nodes/dispatch_performer.py` so the documenter always runs (FR-006).
- [X] T103 [US1] Test in `tests/unit/graph/nodes/test_dispatch_performer.py`: `documenting` with no `docs/` change is **dispatched** (`test_documenting_dispatched_even_without_doc_changes`).

**Checkpoint**: MVP — every card refreshes `docs/wiki/`; the stage is never skipped for a code-only PR.

---

## Phase 3: User Story 2 — symphony-init wiki bootstrap gate (P2)

**Goal**: A brand-new symphony seeds `docs/wiki/` (via our own documenter, `doc_mode="init"`) before non-doc work is dispatched; the seed PR auto-merges on CI-green + trusted-bot approval; the marker is restart-safe; exhaustion holds + notifies.

### Foundation (landed + unit-tested)

- [X] T019 [US2] Schema-v13 round-trip test (`tests/unit/test_state_store_schema_v13.py`): old v12 snapshot loads with wiki defaults; new fields persist/restore.
- [X] T021 [US2] `WikiInitService` test (`tests/unit/services/test_wiki_init.py`, mock github): CI-green + trusted-bot approval → `squash_merge` → `wiki_initialized=True`; `merged=False` → hold + one dedup notification; budget exhausted → `wiki_exhausted=True` + notification.
- [X] T022 [US2] Schema v12→13 + wiki fields on `EnvCacheStateSnapshot` (`state_store.py`) + persist/restore in `daemon.py`.
- [X] T023 [US2] Transient `wiki_in_flight` on `EnvCacheState` (`models/env_cache.py`).
- [X] T024 [US2] `EventType.wiki_init_exhausted` (`models/notification.py`).
- [X] T025 [US2] `WikiInitService` (`src/coordinare/services/wiki_init.py`): detect wiki-absent via `docs/wiki/README.md` on the default branch; `check_and_trigger` (dispatch our documenter with `doc_mode="init"`); auto-merge (`check_mergeability` + trusted-bot review + `squash_merge`); attempt budget + `notify_blocked`. Constructed disabled by default.

### Deferred — the one live-verified seam (single follow-up)

- [ ] T026 [US2] Wire the gate + trigger together (they must land as one to avoid a deadlock): re-add `wiki_init_gate_enabled` / `wiki_init_max_attempts` config, seed `WikiInitService` in `__main__`, add the dispatch-hold gate in `dispatch_performer`, and add the per-cycle daemon dispatch/poll (`_execute_wiki_init_dispatch` / `_poll_wiki_init_completion`). Requires a **cardless documenting job that opens a PR** (the documenting terminal path returns `docs_committed` without opening one — `main.py:2864`), validated against a live performer. See `contracts/wiki-init-gate.md`.

**Checkpoint**: New symphonies bootstrap a wiki before work; gate is restart-safe and never deadlocks silently.

---

## Phase 4: User Story 3 — grounded, consistent docs (P3)

**Goal**: Docs are grounded in real repo facts and consistent (one canonical home; the wiki is the record of truth agents read).

- [X] T104 [US3] Grounding + structure discipline are enforced in the T101 persona (no invented facts, cite real paths, README links all sections, no thin stubs).
- [X] T014 [US3] `persona_bench` documenter task + `grade_documenter` verify `docs/wiki/README.md` + section pages are produced (`scripts/persona_bench.py`, `tests/unit/test_persona_bench.py`).

---

## Phase 5: Polish & Cross-Cutting

- [X] T031 [P] Operator guide `docs/operators/wiki-documenter.md` (how it's wired, self-hosted-only, deploy checklist, init-gate status).
- [X] T033 Run `.venv/bin/pytest tests/unit` + `agent/performer/tests/unit` and `.venv/bin/ruff check`; confirm green + no coverage regression.
- [X] T035 [P] Save a project auto-memory pointer for spec 124 (pivot rationale: OpenWiki dropped for tool-calling flakiness / cloud off-limits; living `docs/wiki/` via own documenter; init-gate foundation landed, trigger = T026).

---

## Dependencies & Execution Order
- **Foundational (P1)** → **US1 (P2, MVP)** → **US2 (P3)** → **US3 (P4)** → **Polish (P5)**.
- US1 (the persona + always-run) is the MVP and stands alone.
- US2's foundation is landed + tested; only T026 (gate+trigger, one live-verified seam) remains.
- US3 is satisfied by the US1 persona plus the bench grader.
