# Implementation Plan: Implementer Commit-or-Checkpoint Contract

**Branch**: `069-blocked-notification-rehydration` (bundled) | **Date**: 2026-05-23 | **Spec**: [spec.md](./spec.md)
**Status**: LANDED — retroactive documentation

## Summary

Three-layer fix landed in four commits:

1. **Persona layer** (`92509f6`, `b735967`) — `DEFAULT_INSTRUCTIONS` in
   `src/coordinare/services/persona_service.py` rewritten to a uniform
   Role / Process / Output / Forbidden skeleton. Implementer persona
   adds an "Implementation floor" that calls out zero-source-code DONE
   as a critical failure and requires the PR diff to contain the
   implementation.
2. **Protocol layer** (`f83fb1c`) — both performer and coordinare
   `ProtocolResponse` models gain `partial_progress` as a valid status
   plus `head_before` / `head_after` / `next_focus` fields. Test
   contract assertion updated in `8262291`.
3. **Performer + coordinare wiring** (`f83fb1c`) — performer captures
   branch HEAD pre/post dispatch, parses trailing
   `{"status": "partial_progress", ...}` JSON sentinel (gated on
   `role == implementing`), pushes commits, posts PR comment, returns
   `partial_progress`. Coordinare `monitor_performer` adds a branch for
   `partial_progress` (relay `next_focus`, re-dispatch implementing)
   and a guardrail on `blocked` — when `stage == implementing` and
   `head_before == head_after`, route back to `dispatching` with a
   stronger directive instead of honoring the verdict.

## Technical Context

**Language/Version**: Python 3.11 (coordinare + performer)
**Primary Dependencies**: pydantic v2 (existing), LangGraph (existing) — no new deps
**Storage**: None — `head_before` / `head_after` are per-response, not persisted
**Testing**: pytest unit + protocol contract tests
**Project Type**: split — coordinare side under `src/coordinare/`, performer side under `agent/performer/src/performer/`
**Performance Goals**: No measurable change — sentinel parse is one trailing-JSON regex per turn
**Constraints**: Non-implementer roles MUST be unaffected. Sentinel parser MUST be gated on `role == implementing`. Guardrail MUST be gated on `stage == implementing`.

## Files Touched

```text
src/coordinare/services/persona_service.py        # DEFAULT_INSTRUCTIONS restructure + implementer floor
src/coordinare/protocol/models.py                 # ProtocolResponse: partial_progress status + head fields
src/coordinare/graph/nodes/monitor_performer.py   # partial_progress branch + zero-commit guardrail
src/coordinare/graph/nodes/dispatch_performer.py  # relay next_focus into next directive (if applicable)
agent/performer/src/performer/protocol.py        # mirrored ProtocolResponse fields
agent/performer/src/performer/main.py            # HEAD capture, sentinel parse, PR-comment post

tests/unit/protocol/test_protocol_contract.py    # partial_progress in valid-status assertion
tests/unit/graph/nodes/test_monitor_performer.py # zero-commit guardrail tests
agent/performer/tests/unit/test_main.py          # sentinel parse + HEAD capture
```

## Why Bundled on 069

The implementer-floor failure was discovered during the same live
operator session that surfaced the 069 dedup bug. Both were blocking
the same card flow; splitting them into a new branch would have
extended downtime. The 069 PR description calls out the 070 work
explicitly so the merge record is unambiguous.

## Out-of-Scope (Deferred)

- Performer-side adapter changes for non-implementer roles.
- Migrating other roles to a sentinel protocol.
- Persisting head deltas to the snapshot — they're per-response only.
