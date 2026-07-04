# Implementation Plan: OpenWiki Documenter Backend & Symphony Wiki Bootstrap

> **⚠️ PIVOTED (2026-07 — spec 124).** This document describes the original
> approach: adopting LangChain's **OpenWiki** CLI as a documenter backend. That
> approach was **dropped** (OpenWiki's agentic tool-calling was unreliable across
> every self-hosted model, and cloud models are off-limits for this role).
> The shipped design instead **enhances our own `tech_writer` documenter** to
> maintain a living `docs/wiki/` via the reliable `{files}` JSON contract. See
> **`spec.md`** (the source of truth) and `docs/operators/wiki-documenter.md`.
> Sections below referring to an `openwiki` backend, `openwiki/` output paths,
> or model benchmarking are **historical**.

**Branch**: `124-openwiki-documenter` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/124-openwiki-documenter/spec.md`

## Summary

Introduce **OpenWiki** (LangChain's Node.js repo-documentation CLI) as a new one-shot performer backend (`OpenWikiBackend`) that maintains a living `openwiki/` wiki as a symphony's record of truth. The documenter (`tech_writer` → `documenting`) role keeps the wiki current in-card (pre-merge, committed into each card's PR), and a new **wiki-initialization prerequisite gate** — mirroring the env-bootstrap gate — builds the seed wiki before any non-documentation work is dispatched, auto-merging the bootstrap PR once CI is green and the bot-reviewer approves. Adoption is **POC-gated**: the backend, a written go/no-go proof-of-concept, and a cross-model benchmark ship first; the documenter default flips to OpenWiki only after go/no-go passes, with `hermes`/`gpt-oss:120b` retained as instant-rollback fallback.

**Technical approach** (from research): OpenWiki reaches the LiteLLM gateway via its OpenAI provider (`OPENAI_BASE_URL` + `OPENAI_API_KEY` + `OPENWIKI_MODEL_ID`); confirming that passthrough is the POC's first gate, with a small provider-resolution patch as the preferred remediation. Node.js 22 is already in `Dockerfile.base`, so only `npm i -g openwiki` (in `Dockerfile.full`) is needed. The backend mirrors the `junie`/`claude_code` one-shot CLI pattern and emits the unchanged documenter contract (`files` / `docs_committed`). The init gate mirrors `EnvCacheState`/env-bootstrap exactly (persisted circuit-breaker fields, dispatch-time hold, restart-safe marker, operator notification on exhaustion) plus a bootstrap-PR auto-merge step.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv). Generated/invoked runtime: Node.js 22 LTS (already in `Dockerfile.base`) + the `openwiki` npm CLI.
**Primary Dependencies**: pydantic 2.x (config/models, unchanged surface), langgraph (graph nodes), structlog (observability), gql/aiohttp (GitHub GraphQL: `check_mergeability`, `squash_merge`, `get_pr_reviews`), httpx (performer transport), docker CLI (ephemeral performers), the LiteLLM gateway. **New external dep**: `openwiki` (npm, MIT). No new Python dependencies.
**Storage**: JSON snapshot via `state_store.py` (single-host single-process). `CURRENT_SCHEMA_VERSION` bumped 12 → 13; new backward-compatible wiki-init fields on `EnvCacheStateSnapshot`.
**Testing**: pytest (`.venv/bin/pytest`), ruff (`.venv/bin/ruff check`). Coordinare unit tests under `tests/unit/`; performer-package tests under `agent/performer/tests/` (backend adapters).
**Target Platform**: Linux performer containers (ephemeral) + the coordinare daemon (single-host).
**Project Type**: Existing single-repo backend (coordinare daemon + `agent/performer` package). No frontend work.
**Performance Goals** (SC-008, POC-confirmed): initial wiki build ≤ 20 min; incremental update ≤ 10 min on the benchmark repo. Benchmark also records per-model cost/latency.
**Constraints**: Documenter result contract MUST NOT change (downstream unaffected, SC-009). Secrets never logged (existing `_redact_secrets`). Gated default switch reversible via a single config change (SC-005). Init auto-merge must not deadlock a symphony (attempt-budget + notify, SC-007).
**Scale/Scope**: One new backend module + one prompt builder; ~1 new service + 1 dispatch-gate branch + 1 daemon hook; schema-v13 fields; config additions (1 endpoint, 1 model_endpoint, 1 mode); persona_bench extension (1 role fixture + grader); 1 Dockerfile line; a POC report + benchmark script/run.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. `OpenWikiBackend` is single-responsibility (drive the CLI, parse result, emit events), mirrors the proven `junie`/`claude_code` one-shot pattern, reuses `build_subprocess_env`/`_redact_secrets`. Only one new external dependency (`openwiki` npm), justified as the feature's entire purpose. Public interfaces get type annotations (Protocol already typed). No dead code; the fallback `hermes` block is live config, not commented-out.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Test-first is mandated for this feature (see Phase 1 test list): unit tests for the backend adapter (start/parse/status/stop, init-vs-update arg construction), the doc-skip reconciliation, the wiki-init gate (hold/attempt-budget/exhaustion/restart), config resolution, and the schema-v13 migration; a contract test for the documenter payload + backend output parsing; an integration test for the dispatch-hold gate. Coverage must not regress. Benchmark grading is deterministic + judge (persona_bench conventions).
- **III. User Experience Consistency** — PASS. Operator-facing surfaces reuse existing patterns: the dispatch-hold logs a human-readable reason (mirror `bootstrap_hold_detail`), exhaustion dispatches a `NotificationEvent` (critical, dedup-keyed) like `env_bootstrap_exhausted`, and the dashboard reflects the gate/phase. Error reasons are actionable, secrets stripped.
- **IV. Performance by Design** — PASS. Measurable budgets live in the spec's Success Criteria (SC-008); the POC measures and tunes them; the benchmark records per-model cost/latency; the one-shot subprocess carries a timeout. No new hot path in the daemon loop (gate check is a cheap field read, like env-bootstrap).
- **V. Clarity Before Action** — PASS. Zero `NEEDS CLARIFICATION` markers remain; brainstorming + `/speckit.clarify` resolved rollout gating, in-card update semantics, the auto-merge gate definition, and the gateway-incompat fallback. All resolutions are recorded in `spec.md` (`## Clarifications`).

**One justified deviation** — see Complexity Tracking: auto-merging the init-bootstrap PR is a deliberate, narrowly-scoped exception to coordinare's "only humans approve merges" invariant.

## Project Structure

### Documentation (this feature)

```text
specs/124-openwiki-documenter/
├── plan.md              # This file
├── spec.md              # Feature spec (+ Clarifications)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── openwiki-backend.md      # CLI invocation, env mapping, output parsing
│   ├── documenter-job.md        # doc_mode payload + result contract
│   ├── wiki-init-gate.md        # gate states, dispatch-hold, auto-merge
│   └── benchmark.md             # documenter fixture, grading, verdicts
├── checklists/
│   └── requirements.md          # Spec quality checklist (passing)
└── tasks.md             # /speckit.tasks output (NOT created here)
```

### Source Code (repository root)

```text
agent/performer/
├── src/performer/
│   ├── backends/
│   │   ├── openwiki.py          # NEW — OpenWikiBackend (one-shot CLI adapter) + _build_task_prompt
│   │   ├── __init__.py          # EDIT — register "openwiki" → OpenWikiBackend
│   │   └── base.py              # (unchanged Protocol; reference)
│   ├── models.py                # EDIT — Score.doc_mode: Literal["init","update"] = "update"
│   ├── main.py                  # (documenter alias/contract; reference — may branch on doc_mode)
│   └── protocol.py              # (PerformerResponse; reference)
├── tests/                       # NEW backend unit tests (test_openwiki_backend.py)
└── Dockerfile.full              # EDIT — add `openwiki` to the global npm install

src/coordinare/
├── services/
│   └── wiki_init.py             # NEW — WikiInitService (mirror EnvCacheService): detect/dispatch/complete
├── graph/nodes/
│   ├── dispatch_performer.py    # EDIT — (a) wiki-init dispatch-hold gate (mirror env-cache 1291-1337);
│   │                            #        (b) doc_mode-aware documenting skip (reconcile _should_skip_documenting)
│   └── merge_pr.py              # (reference; auto-merge helper for bootstrap PR lives in wiki_init/service)
├── daemon.py                    # EDIT — start() hook (~2480) to seed WikiInitService; _persist_env_cache wiki fields
├── __main__.py                  # EDIT — instantiate + initialise WikiInitService (mirror ~1009-1017)
├── models/env_cache.py          # EDIT — EnvCacheState wiki-init live/transient fields
├── state_store.py               # EDIT — CURRENT_SCHEMA_VERSION 12→13; EnvCacheStateSnapshot wiki fields
├── models/notification.py       # EDIT — EventType.wiki_init_exhausted
└── config.py                    # (PerformerRoleConfig/Endpoint/Mode reused; no model change expected)

config.yaml                      # EDIT — openwiki-ephemeral endpoint; openwiki mode+model_endpoint; tech_writer switch (gated) + hermes fallback retained
scripts/persona_bench.py         # EDIT — documenter RoleTask + grade_documenter + rubric
tests/unit/                      # NEW/EDIT — gate, dispatch skip, config, schema-migration, notification tests
```

**Structure Decision**: Existing coordinare monorepo layout — the performer backend lives in the `agent/performer` package (baked into the image at build time per `Dockerfile.base`), and the orchestration/gate/state changes live in `src/coordinare`. No new top-level projects. This mirrors how every prior backend (junie/hermes/codex) and every prior gate (env-bootstrap) were added.

## Phased Delivery (maps to spec user stories)

- **Phase A → US1 (P1)**: `OpenWikiBackend` + registry + `Score.doc_mode` + `Dockerfile.full` npm + prompt builder + backend unit/contract tests. MVP: a dispatchable documenter engine that builds/updates a wiki and reports `docs_committed`.
- **Phase B → US2 (P2)**: POC (LiteLLM passthrough → container run → quality → cost/latency; written go/no-go) + `persona_bench` documenter fixture/grader run across the 4 models → ranking + recommended model.
- **Phase C → US3 (P3)**: `WikiInitService` + dispatch-hold gate + daemon/`__main__` wiring + schema-v13 fields + bootstrap-PR auto-merge (scoped exception) + notification + gate/restart tests.
- **Phase D → US4 (P4)**: gated config switch of `tech_writer.backend` to `openwiki` on the benchmark-selected model, hermes retained; rollback documented + config-resolution test.

Phases A→B are the "before we commit" path; C and D land only behind a passing go/no-go. `/speckit.tasks` will decompose these into ordered, test-first tasks.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Auto-merging the init-bootstrap PR bypasses coordinare's "only humans approve merges" invariant (`monitor_pr.py:221`; `check_mergeability.mergeable` requires human `reviewDecision == APPROVED`). | User decision (spec Clarifications Q2 + brainstorming §C): a brand-new symphony must not block on a human merging the seed wiki. The bootstrap auto-merge is gated on required-CI-green **and** a trusted-bot approval, scoped to the single initialization PR only; ongoing wiki updates keep the normal human gate (FR-018). | Hold-for-human (Option A in brainstorming) was rejected because it stalls every new symphony on a person before any work can start. Direct-commit-to-main (Option C) was rejected because it skips CI/review entirely. Branch protection requiring human review can still block the auto-merge → handled as the "auto-merge blocked" edge case (hold + notify), so the exception degrades safely. |
| New `WikiInitService` parallel to `EnvCacheService` (rather than folding into it). | Wiki initialization has a distinct trigger (wiki absent on main) and a distinct completion action (open + auto-merge a seed PR) that env-bootstrap does not; co-locating would violate single-responsibility. | Extending `EnvCacheService` was rejected because it would entangle two unrelated gate lifecycles in one class; the persisted state is co-located on `EnvCacheStateSnapshot` (cheap, restart-safe) but the service logic stays separate. |
