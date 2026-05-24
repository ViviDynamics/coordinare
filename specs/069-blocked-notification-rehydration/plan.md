# Implementation Plan: Block-Notification Dedup on State Rehydration

**Branch**: `069-blocked-notification-rehydration` | **Date**: 2026-05-22 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/069-blocked-notification-rehydration/spec.md`

## Summary

After a coordinare restart, a stale `phase=blocked` snapshot was re-emitting a Slack
`🚫 blocked: needs input` event and a fresh GitHub reminder comment for a card that
was already being re-dispatched. The root causes are three independent gaps:
(1) `PersistedSession` does not carry `last_blocked_notified_at`, so per-card
sessions lose their watermark on rehydration; (2) `notify.py` builds a dedup key
that ignores `open_questions` content and falls back to the literal string
`"needs input"` when questions are empty; (3) neither node consults
`active_sessions` to detect that a fresh performer dispatch has superseded the
stale blocked state.

The fix is scoped to four files (`session.py`/`state_store.py`/`daemon.py`,
`graph/nodes/notify.py`, `graph/nodes/handle_blocked.py`) plus unit tests. No
new dependencies, no schema migration concerns beyond making
`PersistedSession.last_blocked_notified_at` an optional field that v1 snapshots
rehydrate as `None`.

## Technical Context

**Language/Version**: Python 3.11 (coordinare)
**Primary Dependencies**: LangGraph (existing), pydantic v2 (existing), structlog (existing) — no new deps
**Storage**: JSON snapshot via `state_store.py` (`WorkflowSnapshot` + `PersistedSession`). v1 and v2 snapshots must round-trip without loss; new `PersistedSession.last_blocked_notified_at` is optional and defaults to `None` for backward compatibility.
**Testing**: pytest via `.venv/bin/pytest` (unit only — no new integration scaffolding required; User Story 2 is covered by a unit-level reconstruction of the notify/handle_blocked tick against a rehydrated state dict)
**Target Platform**: coordinare daemon process (Linux/macOS)
**Project Type**: single project (coordinare service)
**Performance Goals**: notify-tick latency unchanged (<10ms per card); snapshot serialization size grows by ≤1 ISO-8601 timestamp per persisted session
**Constraints**: Must not break existing v1 snapshots already on disk; must not change Slack/GitHub side effects for cards that *legitimately* re-enter blocked after restart
**Scale/Scope**: ~4 production files modified, ~3 test files extended; no migration script needed

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Note |
|-----------|--------|------|
| I. Code Quality First | PASS | Reuses existing `_SESSION_FIELDS` round-trip pattern in `session.py`; no new abstractions; type hints already present in touched files. |
| II. Testing Discipline (NON-NEGOTIABLE) | PASS | New unit tests for User Stories 1/2/3 + FR-001/FR-002/FR-006 (see `tests/unit/graph/nodes/test_notify.py`, `test_handle_blocked.py`, and a state-store round-trip test). All cases deterministic — no live Slack/GitHub. |
| III. User Experience Consistency | PASS | The operator-facing outcome is the *removal* of a spurious channel — Slack and GitHub semantics for true blocked cards are unchanged. No user-facing copy changes. |
| IV. Performance by Design | PASS | Notify-tick cost is a single dict lookup + sha256 of a small string list; snapshot grows by one timestamp per session. No new I/O. |
| V. Clarity Before Action | PASS | Spec resolves all behavioral questions (which timestamp wins on conflict, what to do when questions are empty, what defines a "fresh" session). No `NEEDS CLARIFICATION` markers remain after Phase 0. |

Performance budget: blocked-notify tick remains <10ms p95 per card (single dict access + one sha256 over a tiny string list); snapshot file size grows ≤80 bytes per active card. Verified by the existing `tests/unit/test_state_store.py` snapshot round-trip timing and a new assertion that snapshot size is within bounds.

No complexity-tracking violations.

## Project Structure

### Documentation (this feature)

```text
specs/069-blocked-notification-rehydration/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── notify-emission.md
│   └── snapshot-roundtrip.md
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── session.py                       # CardSession already carries last_blocked_notified_at; confirm to_dict/from_dict still cover it via _SESSION_FIELDS
├── state_store.py                   # ADD last_blocked_notified_at: datetime | None = None to PersistedSession; serialize from / deserialize into session dicts
├── daemon.py                        # On rehydration, copy per-session last_blocked_notified_at from PersistedSession into the reconstructed session dict
└── graph/nodes/
    ├── notify.py                    # (a) skip card_blocked when open_questions == []; (b) skip when active_sessions[card_id].phase is non-terminal and non-blocked; (c) skip when rehydrated last_blocked_notified_at present + dedup cache empty + question hash unchanged; (d) extend dedup_key with question-content hash; (e) remove "needs input" literal fallback
    └── handle_blocked.py            # Read session-level last_blocked_notified_at (authoritative) before falling back to top-level; treat session-level None as "no prior post" only when top-level is also None

tests/unit/
├── graph/nodes/
│   ├── test_notify.py               # New: empty-questions suppression (US3, FR-003); active-session supersede (US2, FR-005); dedup-key-includes-hash (FR-006); rehydration suppression (FR-004)
│   └── test_handle_blocked.py       # New: session-level timestamp wins over None top-level (FR-001/FR-002); reminder gate honors rehydrated session value (US1)
└── test_state_store.py              # New: PersistedSession.last_blocked_notified_at round-trips; v1 snapshots default to None
```

**Structure Decision**: Single-project layout (existing coordinare service). All edits land under `src/coordinare/` and `tests/unit/`. No new packages, modules, or directories required.

## Complexity Tracking

No constitution violations. Table intentionally empty.

## Scope Extension — Service-Inference Provider Configurability (2026-05-22)

While running the coordinare against this branch we surfaced two issues with
the env_bootstrap service-inference path (spec 063, `_run_service_inference`
in `agent/performer/src/performer/main.py`) that were blocking live runs.
Both are landed on this branch to unblock the operator running ahead of the
069 PR merge; they share no implementation surface with the
blocked-notification dedup work but both touch the performer/inference seam
the operator was exercising when 069's bug was found.

### Phase 1 — `max_tokens` env override (LANDED)

**Problem**: `DEFAULT_INFERENCE_MAX_TOKENS = 100_000` exceeds the
`claude-sonnet-4-5-20250929` 64k output cap, so every env_bootstrap call
returned `PermanentLLMError(400 invalid_request_error)`.

**Change**:
- Lower default to `64_000` (fits Sonnet 4.5 out of the box).
- Read `COORDINARE_INFERENCE_MAX_TOKENS` env var as an integer override
  (parallels existing `COORDINARE_INFERENCE_MODEL` / `…_AGENT_VERSION`
  env overrides). Invalid values log a warning and fall back to the default.
- Three new unit tests in `agent/performer/tests/unit/test_service_inference_helper.py`
  cover override, default, and invalid-value paths.

**Files touched**:
- `agent/performer/src/performer/main.py`
- `agent/performer/tests/unit/test_service_inference_helper.py`

### Phase 2 — Pluggable LLM provider (PLANNED, this branch)

**Problem**: `_run_service_inference` is hardcoded to `ClaudeServiceLLMClient`,
which targets Anthropic directly. Operators with on-prem inference (LiteLLM
proxy fronting vLLM/Ollama) cannot route env_bootstrap to their own hardware.

**Approach**: The `LLMClient` Protocol in
`packages/service_inference/src/coordinare_service_inference/agent.py` is already
the strategy seam — `ClaudeServiceLLMClient` is one implementation. Add a
second implementation targeting **OpenAI-compatible chat-completions**
(covers LiteLLM, vLLM, Ollama-via-LiteLLM, LM Studio, llama.cpp server).

**Functional requirements**:
- FR-A: New `COORDINARE_INFERENCE_PROVIDER` env var (`anthropic` | `openai_compat`), default `anthropic`.
- FR-B: When `openai_compat`, read `COORDINARE_INFERENCE_BASE_URL` and
  `COORDINARE_INFERENCE_API_KEY` (the latter may be empty for unauthenticated
  local proxies; the strategy still sends `Authorization: Bearer ` if a
  value is configured).
- FR-C: Missing `COORDINARE_INFERENCE_BASE_URL` when `openai_compat` is
  selected MUST fail fast with a clear error — do **not** silently fall
  back to Anthropic (that would leak traffic).
- FR-D: Translate the existing tool catalogue from Anthropic schema to
  OpenAI tools schema, and `tool_calls` from the response back into the
  shared `ToolCall` / `LLMStep` types.
- FR-E: Same retry policy — transient (5xx/429/connection/timeout) retries
  via stamina; auth/4xx raises `PermanentLLMError`.
- FR-F: `submit_manifest` MUST be routed to `LLMStep.manifest` for both
  strategies — agent-loop termination must not depend on which provider is
  active.
- FR-G: Token usage (input + output) MUST be captured on `LLMStep` for both
  strategies so cost telemetry continues to work.

**Out of scope (Phase 2)**:
- Switching the **performer card-work** backends (claude_code, hermes, etc.)
  to use this seam — that's a separate larger refactor.
- A YAML `inference:` config block — env vars only for now; YAML can come
  later if the env surface gets unwieldy.
- Additional providers (Bedrock, Vertex) — mechanical once the second
  strategy proves the seam.

**Files to add/touch (Phase 2)**:
- `packages/service_inference/src/coordinare_service_inference/openai_compat_llm_client.py` (new): `OpenAICompatServiceLLMClient` implementing the `LLMClient` protocol via `httpx` (already a transitive dep — no new deps).
- `agent/performer/src/performer/main.py`: provider-selection dispatch in `_run_service_inference`.
- `tests/unit/services/test_service_inference_openai_compat_llm_client.py` (new): fake-HTTP tests mirroring the Anthropic test matrix (tool catalogue, parse, retry, permanent error).
- `agent/performer/tests/unit/test_service_inference_helper.py`: extend with provider-selection tests (anthropic default, openai_compat selected, openai_compat missing base URL fails fast).

**Non-functional**:
- No new mandatory dependencies (`httpx` is already transitive via the GitHub service).
- Default behavior unchanged for operators not setting new env vars.
- All tests run without network.

### Why bundled on 069 instead of a fresh spec

This is a deliberate scope departure from the project's normal "one spec
per branch" discipline. Justification: the operator was actively unblocked
by Phase 1 and asked for Phase 2 on the same branch to keep momentum during
a live debugging session. The 069 PR description will call out both phases
explicitly so the merge record is unambiguous.

---

## Phase 3 — Remove hardcoded Anthropic call sites in the coordinare (2026-05-22)

**Motivation**: With `config.conducting.backend` already configurable across
six backends (anthropic_api, openai_api, claude_cli, codex_cli, opencode,
hermes/none), live testing on the hermes backend revealed two leaks where
the coordinare still reached the Anthropic SDK directly:

1. `__main__.py` built an orphan `ClaudeService` outside the
   `build_conducting_backend` factory and wired it into the advocate scorer
   regardless of `config.conducting.backend`. It only "worked" against
   LiteLLM because the Anthropic SDK happens to honor `ANTHROPIC_BASE_URL`
   and `ANTHROPIC_API_KEY` from the process env — brittle configurability.
2. `ClaudeScorer` (used by the advocate) called `_client.messages.create`
   on the Anthropic SDK directly rather than going through the abstract
   `ConductingBackend` seam.

**Scope (Phase 3)**:
- FR-H: Advocate scoring MUST flow through the configured
  `ConductingBackend`, not a hardcoded Anthropic client.
- FR-I: `CoordinareState.claude_service` (set but never read in `src/`) MUST
  be removed so it cannot regrow into a back-door dependency.
- FR-J: All existing tests for `ClaudeService` and the anthropic_api
  backend MUST continue to pass; no Anthropic-specific behavior should
  regress for operators staying on that backend.

**Out of scope (Phase 3)**:
- `coordinare_service_inference/claude_llm_client.py` configurability (lives in
  the performer image repo).

**Phase 3b — Config-driven `claude_code` / `codex` endpoints (2026-05-22)**:
Added `base_url` + `api_key_env` to `PerformerRoleConfig`. `dispatch_performer`
plumbs them into `card_context`; `HTTPPerformerService` reads the api key from
the env var named by `api_key_env` (defaults: `ANTHROPIC_API_KEY` for
claude_code, `OPENAI_API_KEY` for codex) and injects `ANTHROPIC_BASE_URL` /
`OPENAI_BASE_URL` into the per-job `secrets`. The performer's job-init
handler already mirrors `secrets` into `os.environ`, and the claude_code /
codex CLIs honor those base-URL env vars — so operators can point a per-role
backend at a LiteLLM proxy entirely from `config.yaml` with no container env
plumbing required.

**Files touched (Phase 3)**:
- `src/coordinare/services/scoring.py`: rename `ClaudeScorer` →
  `BackendScorer`; route through `backend.prompt(text,
  response_format="json")`; keep `ClaudeScorer = BackendScorer` alias for
  external callers.
- `src/coordinare/__main__.py`: delete the orphan `ClaudeService` build,
  drop the `ClaudeService` import, drop `claude_service` from the state
  dict, wire the advocate scorer as
  `BackendScorer(conducting_backend, provider_name=config.conducting.backend)`.
- `src/coordinare/graph/state.py`: remove `ClaudeServiceProtocol` and the
  `claude_service` field from `CoordinareState`.
- `tests/unit/services/test_scoring.py`,
  `tests/unit/test_scoring_coverage.py`: rewrite mocks against the
  `ConductingBackend.prompt` interface instead of Anthropic SDK internals.
- `tests/integration/test_graph_execution.py`: drop the now-dead
  `_Claude` fixture and its `claude_service` state entry.

**Non-functional**:
- No new dependencies.
- No YAML schema changes; the existing `config.conducting.*` block already
  drives everything.
- Full suite green (3007 passed, 9 skipped) after the refactor.

---

## Phase 4 — check_board lifecycle hardening (2026-05-22)

Drive-by fixes discovered while exercising the dedup work against the
hermes backend. All target `src/coordinare/graph/nodes/check_board.py`
(plus one in `handle_blocked.py` and one in `handle_system_error.py`).
None of these break the dedup contract, but several were directly
involved in the re-emission incident reproductions.

### Motivation

- `check_board` was overwriting `phase=blocked` with a working phase
  before slot accounting ran, so the blocked card never released its
  concurrency slot — blocked cards looked "stuck" and dispatch couldn't
  pick up the next card. (`04713d5`)
- When the TODO column was empty, `check_board` was clobbering in-flight
  working phases (e.g. `monitoring_performer`) with `idle`, causing the
  next tick to lose track of running performers. (`ed35a8e`)
- Readopted `IN_PROGRESS` cards (cards reopened/re-assigned mid-run)
  were being routed to `monitoring_performer` even though no performer
  was actually running for them — they need a fresh dispatch.
  (`356ea5a`)
- `handle_system_error` was setting `phase=blocked` during its retry
  wait, which short-circuited the retry logic. Keeping
  `phase=system_error` during the wait preserves the retry contract.
  (`56f713b`)
- `check_board`'s blocked-comment-detection path read `top-level
  last_blocked_notified_at`, ignoring the session-level watermark that
  069 just introduced. Fall back to the session value when top-level is
  `None`. (`4b3244c`)

### Files touched

- `src/coordinare/graph/nodes/check_board.py` (×3 fixes)
- `src/coordinare/graph/nodes/handle_system_error.py`
- Matching tests under `tests/unit/graph/nodes/`.

---

## Phase 5 — LLM & env resilience (2026-05-22)

Operator-facing hardening that surfaced during live hermes runs. None
of these are 069's dedup contract, but they were the same incident's
contributing factors: empty env vars, transient backend errors, and
schema mismatches were all firing alongside the duplicate
notifications.

### Motivation & changes

- **Empty env vars treated as set**: `_run_service_inference` was
  passing through empty `COORDINARE_INFERENCE_*` strings as if they were
  valid, producing 401s from LiteLLM. Coerce empty strings to "unset"
  and stamp `agent_version` consistently. (`ae55aab`)
- **Bare `${VAR}` placeholders propagated to docker**: `env_bootstrap`
  was forwarding literal `${LITELLM_MASTER_KEY}` strings (unexpanded)
  to `docker -e`, exporting a literal-dollar value into the performer
  container. Strip any value still containing `${...}` before passing
  it on. (`9eb8a2a`)
- **Performer endpoint env coercion**: `PerformerRoleConfig` was
  rejecting non-string env values (ints, booleans) instead of coercing
  them. Coerce to string before pydantic validation. (`f12c16e`)
- **Transient retry on conducting backend**: `OpenAiApiBackend` and
  `BackendScorer` were raising on transient 5xx/429 instead of
  retrying. Wrap both in stamina retries matching the
  `ClaudeServiceLLMClient` policy. (`4aae480`)
- **Service-inference Anthropic streaming**: long inference calls were
  hitting the 10-minute non-streaming timeout. Switch to streaming for
  Anthropic responses. (`10d5478`)
- **Service-inference tool_result JSON encoding**: tool results were
  not being JSON-encoded for Anthropic's schema, producing 400s on
  multi-step agentic loops. (`095a9d6`)
- **Qwen/LiteLLM arg envelope**: LiteLLM returns Qwen tool calls
  wrapped in an extra `arguments` envelope; unwrap before parsing, and
  expose `COORDINARE_INFERENCE_MAX_TOOL_CALLS` to tune the agent loop
  budget per backend. (`893f7a9`)
- **Shell-template packaging**: `service_inference` shell templates
  weren't shipping inside the wheel. Add to `MANIFEST.in` /
  `pyproject.toml` package data. (`d720d41`)
- **Hermes stdout passthrough**: `HermesBackend` was dropping the
  trailing JSON envelope from stdout, so JSON-emitting roles couldn't
  re-extract their structured output. Surface the full stdout as
  `output`. (`8494214`)

### Files touched

- `agent/performer/src/performer/main.py`
- `agent/performer/src/performer/env_bootstrap.py`
- `src/coordinare/config/performer_endpoint.py`
- `src/coordinare/backends/openai_api.py`
- `src/coordinare/backends/hermes.py`
- `src/coordinare/services/scoring.py`
- `packages/service_inference/src/coordinare_service_inference/claude_llm_client.py`
- `packages/service_inference/src/coordinare_service_inference/openai_compat_llm_client.py`
- `packages/service_inference/MANIFEST.in`, `pyproject.toml`
- Matching tests.

### Docs

- `18d2373` — document `COORDINARE_INFERENCE_*` placeholders in every
  example config (`config.example.*.yaml`, `config.hermes.yaml`,
  `config.yaml`) so operators have a single reference for the seam.

---

## Phase 6 — Implementer feedback & handle_blocked enrichment (2026-05-22)

Targets the coordinare's blocked-handling path and implementer
hand-off. Two of these (`999e0a2`, `53399a1`, `2c308f5`) are core 069
follow-ups; the rest harden the surrounding flow.

### Motivation & changes

- **Handle_blocked dropped generated questions**: `handle_blocked` was
  emitting questions in its return value but not persisting them onto
  `state["active_sessions"][card_id]["open_questions"]`, so the
  downstream `notify` node saw an empty list and fell back to the
  `"needs input"` literal. Persist questions back to state before
  returning. (`999e0a2`)
- **Watermark split**: `handle_blocked` was reusing the
  Slack-delivery watermark for its own reminder cadence, so a
  successful Slack dispatch was suppressing the GitHub reminder
  post too. Split into two timestamps:
  `last_blocked_notified_at` (Slack delivery, set by notify) and the
  `handle_blocked` reminder watermark. (`53399a1`)
- **Flat-phase mirror in ainvoke paths**: single-cycle
  `Graph.ainvoke` paths weren't mirroring top-level `phase` onto the
  active session, so rehydration tests saw a stale session phase.
  Mirror flat phase onto session in those code paths. (`2c308f5`)
- **Implementer CI verification on return**: `f9f3a59` enforces
  "performer must verify repo CI is green before returning DONE" and
  enriches the bounce body with which check failed. Closes the
  feedback loop for spec 043's CI-ownership directive.
- **Issue-comment classification via conducting LLM**: `a19e5bb`
  routes blocker-vs-info classification of new issue comments
  through `config.conducting.backend` with a keyword fallback,
  instead of hardcoded substring matching. Plays into 069 because
  misclassified comments were a separate trigger for the same Slack
  reminder firing.

### Files touched

- `src/coordinare/graph/nodes/handle_blocked.py`
- `src/coordinare/graph/nodes/notify.py`
- `src/coordinare/graph/orchestrator.py` (or wherever the single-cycle ainvoke path mirrors phase)
- `src/coordinare/services/issue_comments.py` (LLM classification)
- `agent/performer/src/performer/main.py` (implementer CI verification)
- Matching tests.

### Why bundled on 069

Same operator-session-momentum reason as Phases 2–3. The 069 PR
description enumerates every commit in these phases so reviewers can
audit them out-of-band if desired.

## Drive-by: env-cache restart survival (`207cede`)

Unrelated to blocked-notification rehydration but landed on this branch
during live testing.

**Problem.** Consumer performers (anything other than `env_bootstrap`)
were being dispatched without the `~/.coordinare/env-caches/<sym>` bind
mount whenever the coordinare had restarted since the last bootstrap. The
in-process `EnvCacheState.cache_dir_ready` flag resets to `False` on
every restart and only flips back to `True` after a fresh bootstrap
completes — so a fully populated cache on disk was being ignored. Worse,
consumers were dispatched anyway with no toolchain, burning tokens.

**Fix.**

1. `env_cache.get_env_volume_for_symphony` and
   `_collect_env_volumes_for_persistent_performer` now gate on the
   on-disk presence of `activate.sh` (the file the bootstrap agent
   writes when it finishes) instead of `cache_dir_ready`. This is the
   authoritative cross-restart readiness signal. Bootstrap dispatches
   stay exempt — that's the run that creates `activate.sh`.
2. `dispatch_performer` holds non-bootstrap dispatches when
   `activate.sh` is missing: it releases the slot and returns state
   unchanged so the card retries on the next pickup cycle. Without this
   gate, the first consumer after a cold start would be dispatched with
   no env-cache mount at all.

**Stale cache policy.** Consumers race a running bootstrap on the
existing on-disk cache rather than waiting — dependency drift resolves
on subsequent cycles, which is strictly better than running cacheless.

**Files touched.**

- `src/coordinare/services/env_cache.py`
- `src/coordinare/graph/nodes/dispatch_performer.py`
- `tests/unit/test_060_env_cache.py`
- `tests/unit/graph/nodes/test_dispatch_performer.py`
