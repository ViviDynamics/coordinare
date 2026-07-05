# Feature Specification: Stage Verdict Memory

**Feature Branch**: `125-stage-verdict-memory`
**Created**: 2026-07-04
**Status**: Draft
**Input**: User description: "Stage verdict memory: skip downstream performer re-runs on unchanged head SHA (per-stage verdict cache + doc-gate SHA keying + shared PR diff fetch)"

## Overview

When a card bounces back to the implementer, every downstream verdict stage (reviewing, security, qa, documenting, closing_review) re-runs from scratch on the next pass — even when the PR head commit is byte-identical to the one that stage already approved. Coordinare keeps no memory of "reviewer approved SHA `abc123`", so a single no-op bounce re-dispatches up to five performers whose input has not changed. The spec-123 documenting gate helps only on the first pass: because it keys on the *whole PR diff vs base*, the moment the tech_writer's own commits add a `docs/` file the gate can never skip again — the stage it gates re-triggers itself on every subsequent cycle. Additionally, the documenting dispatch fetches the same PR diff twice back-to-back (once for the gate, once for prompt injection), and issue-comment classification dedup state is lost on every restart, re-running AI classification over already-processed comments.

This feature gives coordinare a persisted, per-stage verdict memory keyed by head commit SHA, re-keys the documenting gate to "what changed since the last documentation pass", consolidates the duplicate PR-diff fetch, and persists the issue-comment dedup watermark. All four changes share one snapshot schema bump (v13 → v14). Diagnosed in the 2026-07-03 architectural review (findings 1, 2, 8, 24).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Skip verdict stages whose input has not changed (Priority: P1)

A card bounces from closing_review back to implementing for a trivial fix. The implementer pushes one commit; reviewing, security, and qa each pass again on the new head; then the card reaches closing_review. On a later bounce where the implementer pushes *no* new commit (e.g. the feedback was already addressed, or only a PR comment reply was needed), the downstream stages that already issued a passing verdict for the current head SHA are skipped instead of re-dispatched — the card advances directly to the first stage that has not yet passed on this exact head.

**Why this priority**: Highest-frequency efficiency win in the pipeline. Every no-op bounce currently costs up to five full performer dispatches (LLM calls, container time, CI wall-clock) that produce verdicts already on record for the same commit.

**Independent Test**: Drive a card through a full passing lifecycle, force a bounce that leaves the head SHA unchanged, and observe that no downstream verdict stage re-dispatches — each is logged as skipped with a cached-verdict reason and the card advances to the same terminal state as before.

**Acceptance Scenarios**:

1. **Given** a card whose reviewing stage recorded a passing verdict for head SHA `S`, **When** the card re-enters dispatching for the reviewing stage and the PR head is still `S` with no pending feedback for that stage, **Then** the reviewing performer is NOT dispatched, the stage is logged as `skipped: verdict_cached`, and the card advances to the next stage.
2. **Given** a card whose reviewing stage recorded a passing verdict for head SHA `S`, **When** the implementer pushes a new commit making the head `S'`, **Then** the reviewing stage IS dispatched normally (cache miss on SHA mismatch).
3. **Given** a card with a cached passing verdict for the current head but pending relay feedback addressed to that stage, **When** the stage is evaluated for dispatch, **Then** the stage IS dispatched (pending feedback vetoes the skip).
4. **Given** a card with an operator pending-override queued, **When** the overridden stage is evaluated, **Then** the override dispatches normally — the verdict cache never suppresses an operator-requested run.
5. **Given** any error while reading the verdict record or resolving the current head SHA, **When** the stage is evaluated, **Then** the stage IS dispatched normally (fail-open: never skip on uncertainty).
6. **Given** a snapshot written before this feature (no verdict records), **When** coordinare restarts and resumes the card, **Then** all stages dispatch exactly as they do today (no skip until a verdict is recorded post-upgrade).

---

### User Story 2 - Documenting gate keyed to the last documentation pass (Priority: P2)

The tech_writer completes a documentation pass on head SHA `S`. On a later cycle the card bounces and returns to the documenting stage at head `S'`. Coordinare now decides the skip by asking "does the change between `S` (last documented) and `S'` (current head) touch any documentation-relevant path?" — not "does the whole PR touch docs/". A PR whose only `docs/` changes are the tech_writer's own earlier commits no longer re-triggers documentation forever.

**Why this priority**: Completes the deferred spec-123 FR-003 and fixes the self-defeating gate keying, which today defeats the 123 gate on exactly the multi-bounce cards where it matters most.

**Independent Test**: Run a card through documenting once, bounce it with a code-only fix commit, and observe the documenting stage is skipped; bounce it again with a commit that edits a `docs/` file and observe the tech_writer is dispatched.

**Acceptance Scenarios**:

1. **Given** a card whose last documentation pass completed at head `S`, **When** the documenting stage is evaluated at head `S'` and the diff `S..S'` touches no `docs/` path, **Then** the tech_writer is NOT dispatched and the stage is logged as `skipped: no_doc_changes_since_last_pass`.
2. **Given** a card whose last documentation pass completed at head `S`, **When** the documenting stage is evaluated at head `S'` and the diff `S..S'` adds or modifies a `docs/` path, **Then** the tech_writer IS dispatched.
3. **Given** a card with no recorded documentation pass (first time through), **When** the documenting stage is evaluated, **Then** the existing spec-123 whole-PR-diff gate applies unchanged (skip only when the full PR touches no `docs/` path).
4. **Given** the diff between the last documented SHA and the current head cannot be computed (missing SHA, API failure, force-pushed history), **When** the documenting stage is evaluated, **Then** coordinare falls back to the spec-123 whole-PR-diff gate, and if that also fails, dispatches the tech_writer (fail-open, never skip on an unknown diff).

---

### User Story 3 - One PR-diff fetch per dispatch evaluation (Priority: P3)

When coordinare evaluates a documenting dispatch, it currently fetches the full PR diff twice in sequence — once to compute changed file paths for the gate, once to build the prompt's inline diff — discarding half of each result. A single fetch now serves both consumers.

**Why this priority**: Pure waste with zero behavioural nuance; halves GitHub API diff calls on every documenting dispatch and removes a double-fetch pattern that would otherwise be copied into the new gate logic.

**Independent Test**: Dispatch a documenting stage on a PR with doc changes and count PR-diff API calls: exactly one.

**Acceptance Scenarios**:

1. **Given** a documenting-stage dispatch that passes the doc gate, **When** the performer prompt is assembled, **Then** the PR diff used for prompt injection comes from the same fetch that served the gate (one API call total).
2. **Given** the single fetch fails, **When** dispatch proceeds, **Then** behaviour matches today's independent failure paths: the gate fails open (dispatch) and the prompt omits the inline diff (persona fallback instructs the model to fetch it).

---

### User Story 4 - Issue-comment dedup survives restart (Priority: P4)

Coordinare already avoids re-classifying issue comments it has processed within a session, but that memory is in-process only. After a daemon restart, the first poll cycle re-fetches and re-classifies the entire visible comment backlog through the AI classifier. The processed-comment memory now persists in the snapshot alongside the existing review-ID memory, so a restart resumes with zero duplicate classification calls.

**Why this priority**: Small but strictly-positive fix that shares this feature's snapshot schema bump; symmetric with the already-persisted review-ID dedup.

**Independent Test**: Process a card with N issue comments, restart coordinare, and observe zero AI classification calls for those N comments on the first post-restart cycle.

**Acceptance Scenarios**:

1. **Given** issue comments classified in a prior session, **When** coordinare restarts and polls the board, **Then** those comment IDs are not re-classified (zero AI calls for them).
2. **Given** a pre-upgrade snapshot without the persisted comment memory, **When** coordinare restarts, **Then** it loads normally and behaves as today (one-time re-classification, then persistence takes over).
3. **Given** a long-lived deployment, **When** the persisted comment memory grows, **Then** it is bounded (oldest entries evicted beyond a fixed cap) so the snapshot cannot grow without limit.

---

### Edge Cases

- **Force-push / rebase**: any history rewrite changes the head SHA, so cached verdicts and the last-documented SHA miss naturally; stages dispatch normally. A force-push *back to* a previously-approved SHA legitimately hits the cache — the commit content is identical by construction.
- **Persona or model reconfiguration mid-card**: a cached verdict predates the config change. Accepted risk for this feature's scope: the cache key is the head SHA only; operators who change a stage's persona/model and want a re-run on an unchanged head use the existing override mechanism (which always dispatches, scenario US1-4).
- **Security stage**: skipping a cached-pass security stage also skips its dispatch-time scanner floor. This is sound because the scanner is deterministic over the same diff — same head, same findings. A cached *failing* verdict never skips (only passing verdicts are cached).
- **Concurrent verdict sources**: a stage verdict recorded by one lifecycle pass must never be consulted by a different card or a different PR (records are per-card, per-stage, single-slot — a new verdict for a stage overwrites the old one).
- **Clock/ordering**: verdict records carry a timestamp for observability only; skip decisions compare SHAs, never times.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST record, per card and per verdict stage (reviewing, security, qa, documenting, closing_review), the PR head commit SHA and verdict outcome each time that stage completes with a passing terminal status. The record MUST survive restart.
- **FR-002**: Before dispatching a verdict stage, coordinare MUST skip the dispatch and advance the card as if the stage completed when ALL of the following hold: (a) a recorded passing verdict exists for that stage, (b) the recorded SHA equals the current PR head SHA, (c) no pending feedback or operator override targets the card. Otherwise it MUST dispatch normally.
- **FR-003**: The skip decision MUST fail open: any error, missing record, unresolvable head SHA, or ambiguity results in a normal dispatch. A skip MUST never occur on uncertainty.
- **FR-004**: The implementing and assessing stages MUST never be skipped by this mechanism.
- **FR-005**: Every skip MUST be observable: a structured log event carrying card ID, stage, head SHA, and reason, distinguishable from spec-123's existing `no_doc_changes` skip.
- **FR-006**: When the documenting stage completes successfully, coordinare MUST record the head SHA at which documentation was last produced (the "last documented SHA").
- **FR-007**: When a last documented SHA exists, the documenting gate MUST key on the diff between that SHA and the current head: skip when that diff touches no documentation path, dispatch when it does. When no last documented SHA exists, the spec-123 whole-PR-diff gate applies unchanged.
- **FR-008**: If the between-SHAs diff cannot be computed, the gate MUST fall back to the spec-123 whole-PR-diff check, and if that also fails, dispatch the tech_writer (never skip on an unknown diff).
- **FR-009**: A single dispatch evaluation MUST fetch the PR diff at most once; the changed-file list and the raw diff text MUST be derived from the same fetch.
- **FR-010**: Processed issue-comment identifiers MUST persist in the snapshot and be restored on restart, bounded to a fixed maximum count with oldest-first eviction.
- **FR-011**: The snapshot schema version MUST be bumped once for this feature; snapshots written at any prior version MUST load with all new fields empty and produce behaviour identical to today until new records accumulate.
- **FR-012**: With no cached verdicts, no last documented SHA, and no persisted comment IDs, coordinare's dispatch behaviour MUST be byte-identical to the pre-feature baseline.

### Key Entities

- **Stage verdict record**: per-card, per-stage single slot holding {head SHA, verdict outcome, recorded-at timestamp}. Overwritten on each new passing verdict for that stage. Consulted only by the skip decision for the same card and stage.
- **Last documented SHA**: per-card marker of the head commit at which the most recent documentation pass completed. Consumed by the documenting gate.
- **Persisted comment watermark**: bounded collection of already-classified issue-comment identifiers, symmetric with the existing persisted review-ID collection.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card that bounces with an unchanged head SHA dispatches zero downstream verdict-stage performers for stages that already passed on that head (today: up to five redundant dispatches per such bounce).
- **SC-002**: Every skipped stage is auditable: 100% of skip events pair with a prior recorded passing verdict for the same card, stage, and head SHA.
- **SC-003**: On a multi-bounce card whose later commits touch no documentation, the tech_writer dispatch count is at most 1 + the number of bounces that changed documentation paths (today: one dispatch per bounce once any `docs/` file exists in the PR).
- **SC-004**: PR-diff API calls per documenting dispatch drop from two to one.
- **SC-005**: After a restart, zero duplicate AI classification calls occur for issue comments processed before the restart.
- **SC-006**: A fresh card (no records) behaves identically to the pre-feature pipeline across a full lifecycle (regression suite equivalence).

## Assumptions

- "Passing terminal status" per stage means the existing terminal success markers coordinare already recognises for that stage (reviewer approval, security pass, QA pass, docs committed, closer approval). This spec adds no new verdict types.
- The current PR head SHA is resolvable at dispatch-evaluation time from state coordinare already tracks (PR head bookkeeping recorded by prior stages and mergeability checks); when it is not resolvable, FR-003's fail-open applies.
- The documentation-path predicate remains spec-123's `docs/` prefix rule. Broadening it (README, `*.md` outside `docs/`, per-symphony configurability) is explicitly out of scope for this feature.
- Cached verdicts are not invalidated by persona/model/config changes (see Edge Cases); the operator override path is the escape hatch.
