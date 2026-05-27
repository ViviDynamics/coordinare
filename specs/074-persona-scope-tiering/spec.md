# Feature Specification: Persona Scope Tiering

**Feature Branch**: `074-persona-scope-tiering`
**Created**: 2026-05-27
**Status**: Draft
**Input**: User description: Right-size per-persona effort to PR complexity so coordinare doesn't spend full-depth reviewer/security/qa/documenter passes on trivial changes (docs typos, config defaults) or shallow passes on risky changes (migrations, auth paths). Stack-agnostic; configurable per project.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Docs-only PR skips irrelevant personas (Priority: P1)

A card changes only `*.md` files (e.g., README update, ADR addition). Today every persona — reviewer, security, qa, documenter, closer — runs at full depth, spending 15–25 tool calls each on a change that has no runtime impact. Operators waste real wall-clock time and inference tokens watching qa explore a codebase that wasn't touched.

**Why this priority**: This is the highest-frequency case where current behavior wastes the most effort relative to risk. Solving it alone delivers a visible throughput improvement on the live test queue.

**Independent Test**: Submit a docs-only card; verify reviewer runs in `skim` mode (≤5 tool calls), security/qa are marked `skip`, documenter runs `full`, closer runs as today. Card reaches IN_REVIEW measurably faster than the baseline.

**Acceptance Scenarios**:

1. **Given** a card whose diff touches only files matching the configured `docs` path class, **When** coordinare dispatches it to the persona pipeline, **Then** each persona receives a `PersonaScope` with depth `skim` or `skip` per project config, and each persona's prompt is augmented with the assigned focus.
2. **Given** the same docs-only card, **When** the security and qa personas would have run, **Then** they are skipped entirely and their stage transitions advance without invoking the performer.
3. **Given** the docs-only card reaches the closer, **When** closer runs, **Then** depth is ignored (closer is scope-invariant) and only the `focus` field is surfaced as advisory context.

---

### User Story 2 — Security-sensitive paths force full depth regardless of size (Priority: P1)

A 6-line change in `src/coordinare/auth/` or a new migration file. By LOC/file-count heuristics this would qualify as `skim`, but the path class is security-sensitive and a missed nuance has high blast radius.

**Why this priority**: Without this override, the feature trades safety for speed in exactly the cases where speed is least valuable. Path-class forcing is the safety net that lets the rest of the system be aggressive.

**Independent Test**: Submit a small diff (<20 lines) that touches a `security_sensitive` path class; verify the classifier's output for the security persona is overridden to `full` regardless of LLM judgment, and a structured log records the override reason.

**Acceptance Scenarios**:

1. **Given** a card whose diff includes any file matching the `security_sensitive` path class, **When** PersonaScope is computed, **Then** the security persona's depth is `full` and the override is recorded with `reason: "forced_full_on_path_class:security_sensitive"`.
2. **Given** the same card, **When** other personas are assessed, **Then** they are not affected by the security override (a 6-line auth-touching docs change still lets documenter run as appropriate).

---

### User Story 3 — Classifier failure falls back to today's behavior (Priority: P1)

The coordinare backend errors, times out, or returns malformed output when asked to classify a card. The card must continue to flow.

**Why this priority**: A new feature must not be able to stall the pipeline. The fallback contract is what lets operators adopt this without operational anxiety.

**Independent Test**: Inject a classifier failure (mock backend timeout); verify the card proceeds with `PersonaScope` set to `full` for every persona, a single warning is emitted to logs and Slack, and the warning is rate-limited to avoid noise during sustained outages.

**Acceptance Scenarios**:

1. **Given** the classifier call fails for any reason (network error, timeout, JSON parse failure, refusal), **When** coordinare proceeds with the card, **Then** every persona runs at depth `full` (today's behavior) and a `persona_scope_classification_failed` warning is emitted.
2. **Given** repeated classifier failures within a configurable window, **When** warnings would be emitted, **Then** they are deduplicated per project at the NotificationService's standard cooldown.

---

### User Story 4 — Re-assessment each cycle keeps scope current (Priority: P2)

A card starts as a 12-line config change (classified `skim` across the board), but the implementer comes back with a 240-line diff that adds a new endpoint. The scope used by reviewer/security/qa on this cycle must reflect what the diff *now* looks like, not what it looked like at first dispatch.

**Why this priority**: Re-assessment matters less than the base classification — most cards' diffs don't change category mid-flight — but without it the feature silently under-scopes the cases where it matters most.

**Independent Test**: Simulate a card whose diff grows from `skim` size to `full` size between two cycles; verify the second cycle's PersonaScope reflects the new diff and personas receive the updated focus.

**Acceptance Scenarios**:

1. **Given** an active card whose PersonaScope was last computed on a prior cycle, **When** coordinare enters the persona dispatch step on a new cycle, **Then** PersonaScope is recomputed against the current diff and persisted to the session.
2. **Given** classifier latency or transient failure, **When** recomputation would block dispatch, **Then** the previous cycle's PersonaScope is reused (with a debug log) so the cycle still completes.

---

### User Story 5 — Project author configures path classes and per-persona scope behavior (Priority: P2)

A new project (Go service, Rails app, Python CLI) adopts coordinare. The author needs to tell coordinare which file globs count as docs, config, tests, runtime, security-sensitive — and how each persona interprets `skim`/`normal`/`full`/`skip` — without touching coordinare source.

**Why this priority**: The feature is worthless if it only works on coordinare itself. Portability is the load-bearing requirement.

**Independent Test**: Configure a fresh project with custom path-class globs and per-persona `scope_behavior`; verify classification respects the project's taxonomy and personas honor the project's behavior overrides.

**Acceptance Scenarios**:

1. **Given** a project config that defines `persona_scope.path_classes` with project-specific globs, **When** coordinare classifies a card, **Then** the project's globs are used (no hardcoded path assumptions).
2. **Given** a project config where a persona omits `scope_behavior`, **When** that persona runs, **Then** it ignores PersonaScope entirely and runs as today (additive default).
3. **Given** a persona's `scope_behavior.skim` defines a `max_tool_calls` budget and `prompt_addon`, **When** the persona is dispatched with `depth: skim`, **Then** both are applied to the persona's invocation.

---

### Edge Cases

- **Empty diff or no GitHub PR yet**: A card mid-implementer with no PR open and no diff to classify. Classifier should be skipped (not failed); PersonaScope defaults to `full` everywhere. This is identical to today's behavior — no regression.
- **Diff spans multiple path classes**: A card touches docs + config + runtime. Precedence: any security-sensitive match forces security to full; otherwise the most-restrictive (highest-depth) class wins per persona.
- **Diff too large for the classifier's context window**: Pass only path summary + diff-stat (file list + ±LOC per file), never the full diff. The classifier never sees raw code.
- **Persona prompt template missing a `{{ scope.focus }}` placeholder**: Persona gets the scope object but doesn't render it. Not an error — older persona templates remain valid. Operator can add the placeholder when ready.
- **Classifier emits an unknown depth value**: Treated as a malformed output → classifier failure → fallback to `full` everywhere with a warning.
- **Closer override**: closer always ignores `depth` and consumes only `focus` (per US1 #3). If a project config sets `respects_depth: true` for closer, it is ignored with a startup warning.
- **PersonaScope persistence**: PersonaScope rides on `CardSession` and must round-trip through `_SESSION_FIELDS` (regression-prone — see recent 069/072 fixes). v1 snapshots without PersonaScope rehydrate as if classifier had not yet run; the next cycle recomputes.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST compute a `PersonaScope` object for each active card before dispatching to any persona. The object maps each persona name (reviewer, security, qa, documenter, closer) to `{ depth: skim|normal|full|skip, focus: <string> }`.
- **FR-002**: The classifier MUST consume only deterministic inputs (file paths, ±LOC per file, project's path-class config) and the project's existing CLAUDE.md/AGENTS.md context — never the raw diff body — when calling the LLM.
- **FR-003**: Coordinare MUST recompute PersonaScope on every cycle for every active card (per design decision: every cycle, reusing the existing coordinare backend).
- **FR-004**: Path-class matches configured under `persona_scope.path_classes.<class_name>` MUST be glob-based and project-configurable; no path patterns may be hardcoded in coordinare source.
- **FR-005**: A persona listed in `persona_scope.forced_full_on_path_classes` MUST be forced to depth `full` whenever the card's diff touches any file in those classes, regardless of LLM output. Each forced override MUST log a structured reason.
- **FR-006**: When the classifier call fails (network error, timeout, malformed JSON, schema violation, model refusal), coordinare MUST proceed with `depth: full` for every persona and emit a `persona_scope_classification_failed` warning (rate-limited per project via existing notification cooldowns).
- **FR-007**: Each persona's invocation MUST receive its scope slice as a structured input separate from the persona's base system prompt. Persona base prompts MUST NOT be mutated by coordinare.
- **FR-008**: When a persona has `depth: skip`, coordinare MUST advance the lifecycle past that persona without invoking the performer for it. Logs MUST record the skip and its rationale.
- **FR-009**: The closer persona MUST ignore the `depth` field of its PersonaScope slice and consume only `focus` as advisory context.
- **FR-010**: Personas without a `scope_behavior` block in project config MUST run as if PersonaScope did not exist (additive default; backward compatible).
- **FR-011**: PersonaScope MUST be carried on `CardSession` and MUST round-trip through `_SESSION_FIELDS` / `session_to_state` / `state_to_session` without loss. v1 snapshot rehydration MUST treat a missing PersonaScope as "not yet computed."
- **FR-012**: PersonaScope MUST be surfaced on the card's PR (as a comment or status-line) so reviewers can see *why* a persona ran shallow or was skipped. The exact surface (PR comment vs. dashboard) is an implementation choice but it MUST be visible to a reviewer reading the PR.
- **FR-013**: The classifier prompt MUST emit `focus` as a free-text string per persona (one to three sentences). Structured tag vocabularies are out of scope for this feature.
- **FR-014**: The classifier MUST run on coordinare's existing configured backend; no new `classifier_backend` config slot is introduced.
- **FR-015**: When a project's classifier produces output that is well-formed but specifies an unknown persona name, coordinare MUST ignore the unknown persona and emit a debug log; remaining personas in the output are honored.
- **FR-016**: Classifier latency budget per call MUST be configurable (default 30s); exceeding it triggers the FR-006 fallback path.

### Key Entities

- **PersonaScope**: Per-card classification produced by coordinare's classifier. Shape: `{ <persona_name>: { depth: skim|normal|full|skip, focus: string, overrides: [string] } }`. Lives on `CardSession`; recomputed every cycle; persists in v2 snapshots; absent in v1 (treated as "not computed").
- **PathClass**: Named bucket of file globs defined in project config (e.g., `docs`, `config`, `tests`, `runtime`, `security_sensitive`). Drives both deterministic forced-full overrides and the deterministic input the classifier sees alongside the diff stats.
- **ScopeBehavior**: Per-persona, per-depth definition of how a depth tier is realized — `max_tool_calls` budget and `prompt_addon` text. Lives under `symphony.personas.<name>.scope_behavior` in coordinare config. A persona without a `scope_behavior` block opts out of the feature.
- **ClassificationFailure**: Sentinel state when the classifier call cannot produce a valid PersonaScope. Causes the card to run with `full` depth across all personas and emits a rate-limited warning.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a docs-only card (≤20 LOC across `*.md` files), end-to-end wall time from `dispatching` to `monitoring_pr` is reduced by ≥40% versus baseline (today's behavior) on the same hardware.
- **SC-002**: On a security-sensitive card (any file in the `security_sensitive` class), the security persona always runs at depth `full`, verified by a structured log entry on every such card.
- **SC-003**: A simulated classifier failure on a representative card does not increase end-to-end latency by more than 30s (the configurable budget) and never blocks the card from reaching `monitoring_pr`.
- **SC-004**: A reviewer reading a closed PR can determine, from PR-visible artifacts, what depth each persona ran at and the focus rationale, without reading coordinare logs.
- **SC-005**: Adopting persona-scope tiering on a fresh project (different stack) requires only adding `persona_scope` config and `scope_behavior` blocks to existing persona configs; no code changes to coordinare.
- **SC-006**: Per-cycle classifier overhead is ≤2s p50 / ≤10s p95 on coordinare's existing backend, measured across at least 20 cards spanning the path-class spectrum.

## Out of Scope

- **Structured tag vocabulary for `focus`**: free-text only this iteration; structured tags may layer on top later without breaking the schema.
- **A dedicated classifier backend config slot**: classifier reuses coordinare's existing backend.
- **Learning loop / closed-feedback on classifier quality**: no automated tuning of classifier prompts from human-review outcomes. This is a manual-tuning feature for now.
- **Cross-card / queue-wide scope decisions** (e.g., "this week the security team is heads-down, raise security depth everywhere"): scope is per-card only.
- **Persona-internal budgeting beyond `max_tool_calls` and `prompt_addon`**: e.g., changing model, temperature, or context window per depth tier — deferred.
- **Backfill of PersonaScope onto already-merged PRs**: only forward-going cards are classified.
