# Implementation Plan: Notification Channels Fully Optional

**Branch**: `139-optional-notification-channels` | **Date**: 2026-08-29
**Spec**: [spec.md](./spec.md) | **Issue**: [#187](https://github.com/ViviDynamics/coordinare/issues/187)

## Summary

Make a partly-configured or dangling notification channel a degradation rather than a boot failure,
report the resulting posture in one startup line, and log a stall unconditionally so it is visible
with no channels and no dashboard. Delivery is untouched; it is already resilient.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12).

**Primary Dependencies**: none added. Touches `config.py` (notification models — read, not
loosened), `services/notification.py`, `daemon.py`'s stall watchdog, and `__main__.py`'s startup.

**Storage**: none. No persisted state, no schema change.

**Testing**: `tests/unit/test_139_optional_notification_channels.py`, following the spec-146/147
precedent.

**Project Type**: Brownfield behaviour change in a non-essential subsystem.

**Constraints**: must not change delivery behaviour; must not relax validation for any other
subsystem; `config validate` must keep reporting these as errors.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality First | The degradation is expressed once, where the channel set is built, rather than as `try`/`except` scattered through the notification service. |
| II. Testing Discipline | Tests precede the change. The "still fails in `config validate`" case is as important as the "no longer fails at startup" case, and both are written first. |
| III. UX Consistency | One startup line, in the existing structured-log idiom. Skipped channels are named, never silently dropped. |
| IV. Performance | Not applicable. |
| V. Observability | This *is* the observability work: a stall becomes visible without any optional subsystem. |

**Gate: PASS.**

## Project Structure

```
specs/139-optional-notification-channels/
├── spec.md, plan.md, research.md, tasks.md
└── checklists/requirements.md

src/coordinare/
├── services/notification.py      # build the channel set tolerantly; report what was skipped
├── daemon.py                     # log the stall unconditionally, deduped
└── __main__.py                   # emit the posture line once at startup

tests/unit/test_139_optional_notification_channels.py
```

**Structure Decision**: the tolerance lives in the notification service, which is the one component
that already knows what a channel is. `config.py` keeps its rules so `config validate` keeps
working; `daemon.py` and `__main__.py` gain a log line each.

## Implementation Phases

**Phase 1 — Tests first.** A channel missing a field, a dangling routing entry, both still failing
`config validate`, the posture line's content and its absence of secrets, the stall log with
nothing else configured, and its deduplication.

**Phase 2 — Tolerant construction.** Build the active channel set from entries that validate;
collect the rest with reasons.

**Phase 3 — The posture line.** Derived from that set, emitted once at startup.

**Phase 4 — The stall log.** Warning level, unconditional, reusing the existing dedup key.

**Phase 5 — Documentation.** Config guidance stating that channels are optional and naming the
always-available surface.

**Phase 6 — Verification.** Full suite, lint, acceptance re-read, scope check, commit.

## Complexity Tracking

| Item | Why it is not simpler |
|---|---|
| Validation stays strict while the daemon is lenient | The obvious simplification — delete the rule — fixes the daemon and blinds `config validate` at the same time, trading a loud failure for a silent typo. |
| A dedup key shared with the notification path | Two independent dedup policies for the same event will disagree eventually, and the disagreement would show up as either log spam or a missing warning. |

## Risks

- **Relaxing validation reads as a licence to relax it elsewhere.** Mitigated by stating the test
  in the spec: does proceeding do the wrong work, or merely tell nobody? Only the latter degrades.
- **A skipped channel goes unnoticed** — the failure this replaces was at least loud. Mitigated by
  the per-channel report and the posture line, both of which name it.
