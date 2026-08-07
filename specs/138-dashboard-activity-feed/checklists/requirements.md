# Specification Quality Checklist: Dashboard Activity Feed

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-07-30
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Validation Notes

**Iteration 1 findings and fixes:**

1. *Implementation leakage* — the source feedback named the SSE event kind, CSS class names, `DashboardStore`, and the 100 ms watcher. The spec restates these as capabilities ("a distinct, append-only stream", "the dashboard's existing activity-type styling", "the mid-cycle watcher") without naming the mechanisms. Concrete transport and naming decisions are deferred to `/speckit.plan`.

2. *Unmeasurable requirement* — an early draft of FR-020 said retention must be "enough history to be useful". Rewritten to tie the bound to the stuck-threshold window, which is a concrete, testable duration, with the numeric bound left to the plan.

3. *Ambiguous ordering* — "newest-first" alone is untestable when timestamps collide at sub-second granularity. FR-007 now additionally requires a stable, deterministic relative order.

**Deliberately resolved by assumption rather than clarification** (all three had a defensible default; none changes scope enough to block):

- Retention size → Assumption 2, constrained by FR-020.
- Restart behavior → Assumption 1, with FR-018 requiring the UI to disclose it so an empty feed is not misread.
- Backfill on connect → resolved as *yes* (FR-003); without it a tab opened after a stall would not show the stall, which would defeat User Story 2.

**Scope boundary verified against the codebase.** The loss is at *delivery*, not detection — this shapes FR-009/FR-011/FR-012:

- `src/coordinare/daemon.py:3137` — stuck detection carries a `notification_service is not None` gate, but `build_notification_service` (`services/notification.py:315-342`) returns a service even with zero channels and `_bootstrap_services` always populates it (`__main__.py:752, 844`), so the gate is never false in production. Detection runs.
- `src/coordinare/services/notification.py:193-206` — `dispatch()` finds no routing, logs `notification_unrouted`, and returns. **This is where the signal is lost.**
- `src/coordinare/graph/nodes/check_board.py:527-535` — recovery detection is likewise ungated; only the dispatch at `:534` is guarded.

Giving detection a second destination that exists unconditionally therefore falls inside this spec (it is what makes the feed answer the feedback), while making a zero-channel setup first-class end to end remains spec 139. The Out of Scope section states this split explicitly.

**Threshold defaults verified** (informing Assumptions 3 and 4):

- `src/coordinare/config.py:308` — `stuck_alerts.threshold_seconds` defaults to 1800 (enabled), with 3600 s per-phase overrides for the monitoring phases.
- `src/coordinare/config.py:1675` — `stall_timeout_seconds` defaults to 0 (disabled).

The asymmetry is called out in Assumption 4 so planning does not assume the stall watchdog answers the default-configuration case.

## Clarification Session 2026-07-30 — re-validation

Five questions asked and integrated. All checklist items still pass. The session materially changed the spec rather than merely annotating it:

- **Duplicate suppression (FR-022–024)** — closed a correctness hole. Backends re-report their full accumulated event list each poll (`monitor_performer.py:3543`), so a feed derived naively from it would scroll briskly *while an agent was wedged* — the exact inverse of the signal the feature exists to give. SC-009 now tests for it.
- **Liveness indicator (FR-025–027)** — a dropped stream and an idle system previously looked identical, reintroducing the same false negative with more confidence behind it.
- **Quiet marker (FR-028–033)** — the scope decision of the session. Verified defaults meant a stock install would show nothing for 60 minutes (`config.py:311-312` overrides the 30-minute default to 3600 s for exactly the monitoring phases where wedging happens, and `config.py:1675` leaves the stall watchdog at 0/disabled). Without this the feature would not have answered the original report on a default install. **Assumption 4 and the stall-watchdog edge case were replaced, not appended to** — the earlier claim that the stuck entry was the default-config answer is gone.
- **Retention cap (FR-019–021, FR-034)** — replaced the unquantified "normal activity volume" with ~2000. **Corrected during cross-artifact analysis**: the original 20 × 100 derivation was wrong, because the per-card source cap bounds what is in flight at one instant while the feed accumulates every distinct event over time. 2000 is a *memory budget* (2000 × ~200 chars < 1 MB); FR-020/SC-005 now scope the history-depth guarantee to the default concurrency and state that at 20 cards the budget is shared and depth degrades. SC-005 is verifiable as restated.
- **Per-entry truncation (FR-035–037)** — caught a self-inflicted contradiction: the count cap agreed one question earlier does not bound memory if entry text is unbounded, so FR-034 was not yet true as written. Truncation at ingest makes count × size a real ceiling.

Requirements grew from 21 to 37 and success criteria from 8 to 13, all contiguous and non-conflicting. Two supersessions were applied as replacements, leaving no obsolete text — verified by grep for the stale phrases.

**Residual risk accepted**: routine progress entries have no per-card rate limit beyond duplicate suppression, so a chatty card can age out a quiet card's history within the global cap. Called out in the Edge Cases section and judged acceptable at the supported concurrency; revisit if it shows up in practice.

**Not asked, low incremental impact**: accessibility and localization (inherits the existing dashboard's bar); privacy of agent text on an unauthenticated dashboard (the same text is already rendered by the existing performer-log panel, so the feed adds no new exposure); observability of the feed itself.

## Notes

All items pass. Five clarifications integrated; no open questions remain. Ready for `/speckit.plan`.
