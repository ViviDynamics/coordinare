# Feature Specification: Documenter side runs count against max_concurrent_cards

**Feature Branch**: `171-side-run-budget`
**Created**: 2026-10-14
**Status**: Draft
**Input**: Issue #171. `_dispatch_documenting_side_runs` dispatches a documenter side run with no pipeline-slot check, so the number of running performers can exceed `max_concurrent_cards`. Decide, for each daemon-initiated dispatch, whether it sits inside the budget or outside it, and say so in the docs.

## Context

`max_concurrent_cards` is a card budget. The config comment, `docs/onboarding/02-architecture.md` and the spec-179/186 rules all describe it the same way: a card holds one pipeline slot while it has work under way, a card parked on a human review holds none, and `max_cards_in_review` caps the parked cards. The slot is per card, so a card with a lifecycle performer and a side writer is one card.

Spec 165 runs the documenter beside the lifecycle, once per blueprint hash, for a card past architecting. The side run is dispatched from the daemon. That dispatch never consulted the budget. The card it documents is often parked on review, which holds no slot, so the side run added a performer on top of a full budget. The side writer also counts as running work in `select_pipelines` once it exists, so the budget was already inconsistent in both directions.

Four other dispatches also start performer containers from the daemon. Architect and assessor intake are not among them: they run as lifecycle stages through `dispatch_performer`, which already takes a slot. The four card-less dispatches are listed below.

## Decisions

| Dispatch | Where | Inside the budget | Why |
| --- | --- | --- | --- |
| Lifecycle stages, including architect and assessor intake | `dispatch_performer` node | Yes (already) | They belong to a card and take its slot through `dispatch_has_pipeline_slot`. |
| Documenter side run (165) | `_dispatch_documenting_side_runs` → `documenting_side.run_cycle` | Yes (this spec) | It documents a card. It takes the card's slot if it has one, or a free slot if the card is parked. |
| Advocate and curator intake (173) | `_maybe_dispatch_intake` | No (documented) | Card-less. Gated by a scan interval and a per-role in-flight marker. |
| Wiki-init (124) | `_execute_wiki_init_dispatch` | No (documented) | Card-less and operator-initiated. Gated by a per-symphony in-flight marker. |
| Env bootstrap (060/088/218) | `_execute_bootstrap_dispatch`, via `env_cache.check_and_trigger` | No (documented) | Card-less. It must finish before any card in the symphony can run, so it cannot wait behind cards. Gated by `bootstrap_in_flight`. |

Option (a), taking a budget slot, was chosen for the documenter because the documenter documents a card, and the card is what the budget counts. The card-less runs have no card to charge, so each one is put outside the budget and says so plainly in the config field description (`ProjectConfiguration.max_concurrent_cards`), in `config.example.yaml`, and in `docs/onboarding/02-architecture.md`.

## User Scenarios & Testing

### User Story 1 - A side run never adds a performer on top of a full budget (Priority: P1)

An operator sets `max_concurrent_cards: 1`. One card is running its lifecycle performer. A second card is parked on a human review and needs a documenter side run. The side run waits. When the running card finishes, the side run starts.

**Independent Test**: `tests/unit/test_171_side_run_budget.py`, `test_full_budget_holds_the_side_run_without_failing_it` and `test_side_run_starts_once_a_slot_frees`.

**Acceptance Scenarios**:

1. **Given** the budget is full, **When** a side run is wanted for a parked card, **Then** it is not dispatched, nothing is recorded as failed, and no extra performer starts.
2. **Given** the slot later frees, **When** the next cycle runs, **Then** the side run starts once.

### User Story 2 - A side run shares its card's slot, or takes a free one (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a card already holds a slot (for example, it is waiting on CI checks), **When** its side run is wanted, **Then** it starts on that slot and no second slot is taken.
2. **Given** a parked card and a free slot, **When** its side run is wanted, **Then** it takes the free slot.
3. **Given** a card whose lifecycle performer is live, **When** its side run is wanted, **Then** it waits until that performer has finished. A side run never starts beside its own card's live performer.

### User Story 3 - Lifecycle work is not starved (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a card that needs a performer again (returning work), **When** a slot is contended by a side run, **Then** the returning card is admitted first.
2. **Given** a side run that is running, **When** a fresh card waits, **Then** the fresh card is admitted when the side run ends. The hold lasts at most the side run's poll timeout (one hour by default).
3. **Given** a running side writer on a card, **When** that card's lifecycle dispatch is requested, **Then** it is admitted on the card's own slot. A side writer never blocks its card's lifecycle.
4. **Given** an open backlog of fresh cards, **When** a slot frees, **Then** a waiting side run is admitted before a fresh card, so the backlog cannot starve it.

### User Story 4 - Card-less runs stay outside the budget (Priority: P2)

**Acceptance Scenarios**:

1. **Given** the budget is full, **When** an advocate or curator intake run is due, **Then** it dispatches, because it is outside the budget.
2. **Given** the budget is full, **When** a wiki-init run is requested, **Then** it dispatches.
3. **Given** the budget is full, **When** an env bootstrap is due, **Then** it dispatches.
4. **Given** the field description, **When** an operator reads `max_concurrent_cards`, **Then** it names all four card-less runs and says they sit outside the budget.

## Review fixes

An independent review of the first cut found three things, now fixed and pinned by tests.

- **The review cap.** A card waiting on a human review that a side run admitted was no longer counted toward `max_cards_in_review`, so a fresh card could be admitted while two cards were in review. The cap now counts every card waiting on a review, whether or not a side writer runs for it (`is_waiting_on_review`). Test: `test_171_review_cap_and_eligibility.py`.
- **Ineligible cards.** The side-run admission ignored eligibility, so a paused, started card kept the one slot and the waiting side run never started. The admission now uses the same eligible set as the lifecycle selection (`_eligibility_map`). Test: `test_171_review_cap_and_eligibility.py`.
- **Visibility.** A waiting side run logged only at debug, and nothing showed it. It now logs once at info (`documenting_side.waiting_for_slot`), records the skip reason `side_run_waiting` for the snapshot (`session_skip_reasons`), and adds an activity-feed entry. The dashboard shows the reason on a card in review. Test: `test_171_side_run_waiting_visibility.py`.

## Requirements

- **FR-001**: A documenter side run MUST be admitted through the pipeline budget before it is dispatched. A card that is not admitted MUST NOT be dispatched, and MUST NOT be recorded as failed.
- **FR-002**: A side run MUST share the slot of a card that already holds one, and MUST take a free slot otherwise.
- **FR-003**: A side run MUST NOT start while its own card has a live lifecycle performer.
- **FR-004**: The budget selection MUST rank a side run after returning work and before fresh work, so neither a returning card is displaced nor a side run is starved by a fresh backlog.
- **FR-005**: A card whose side run cannot be resolved (no repository, no token) MUST NOT be counted as wanting a slot.
- **FR-006**: The card-less runs (env bootstrap, advocate and curator intake, wiki-init) MUST NOT consult or take a pipeline slot. Their own in-flight markers and gates bound them.
- **FR-007**: The docs for `max_concurrent_cards` MUST name the four card-less runs and say plainly that they sit outside the budget.
- **FR-008**: Lifecycle dispatch admission (`dispatch_has_pipeline_slot`) MUST be unchanged for every card.

## Known residual

A side writer keeps `writer_active` and so keeps its card's slot until its termination is confirmed. This is the owned-writer rule (spec 165), which fails closed so that two writers never share a worktree. It was already true before this spec.

Poll errors are not held until restart. A transient poll error records the run as failed with `writer_active` set. The next cycle asks `recover_documenting_side_session` again. A writer that is still reachable is polled again, and the slot frees when the run completes. This is pinned by `tests/unit/test_171_transient_poll_error.py`. `stop_owned_writers` is not on this path: it runs only when the card is paused or moved out of its column, and it clears a writer only when the service confirms the stop.

The hold does last until restart in one case: when `recover` cannot confirm the writer at all (a runtime with no confirmation path, where `recover` returns False). With `max_concurrent_cards: 1` that stalls the pipeline for that card. This predates spec 171. Making that path retry or give up is a follow-up, not part of this change.

Side-run waits are not bounded by `review_slot_hold_seconds`. That bound applies to a card's automation hold in `monitoring_pr` (spec 186). A waiting side run holds no slot and is not on a hold. A running side writer holds its slot until its poll ends, at most one hour (`DEFAULT_POLL_TIMEOUT_S`).

Because a card-less run is outside the budget, the number of performer containers can exceed `max_concurrent_cards` by the number of card-less runs in flight. Each is bounded to one per symphony by its own marker, and the documentation says so.

## Success Criteria

- **SC-001**: With `max_concurrent_cards` set to N, the number of cards holding a slot never exceeds N through a documenter side run.
- **SC-002**: A waiting side run starts in the first cycle after a slot frees.
- **SC-003**: A lifecycle dispatch waits behind a side run for at most one side-run poll timeout, and a side run never waits behind a fresh card.
