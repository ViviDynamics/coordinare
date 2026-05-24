# Implementation Plan: Checkpoint Protocol Extensions + Head-Delta Audit Trail

**Branch**: `072-implementer-checkpoint-extensions` | **Date**: 2026-05-24 | **Spec**: [spec.md](./spec.md)
**Depends on**: 070 (landed)

## Summary

Three independent surfaces, one branch:

1. **Protocol extension** — promote the `partial_progress` sentinel from
   an `implementing`-only signal to an allow-list of
   `{implementing, reviewing, security, qa, documenting}`. Performer-side gate
   in `agent/performer/src/performer/main.py` and coordinare-side
   stage-preservation in `src/coordinare/graph/nodes/monitor_performer.py`
   both move from `== "implementing"` to `in ALLOW_LIST`.
2. **Per-role zero-progress guardrail** — extends FR-070-7 to non-implementer
   roles with a stricter 3-signal trip (head delta AND no bot PR comments
   this turn AND no new clarifications). Implementer guardrail is
   untouched — single-signal is correct there because commits ARE
   progress.
3. **Head-delta audit trail** — `PersistedSession` gains two optional
   string fields (`head_at_dispatch`, `head_at_last_turn`) that survive
   restarts and round-trip through v1/v2 snapshots. Mirrored into
   top-level state for single-card legacy paths, matching the
   `last_blocked_notified_at` pattern.

Three new integration tests replace the "Manual Validation" items from
spec 070 with deterministic regression coverage.

## Technical Context

**Language/Version**: Python 3.11 (coordinare + performer)
**Primary Dependencies**: pydantic v2, LangGraph, structlog — all existing
**Storage**: JSON snapshot via `state_store.py`. v1 and v2 snapshots
must round-trip without loss; new `PersistedSession` head fields default
to `None`.
**Testing**: pytest unit + new integration tests under
`tests/integration/test_072_*.py`
**Project Type**: split — coordinare under `src/coordinare/`, performer
under `agent/performer/src/performer/`
**Performance Goals**: No measurable change. PR-comment-delta detection
is a single GitHub list call already made by the performer per turn.
**Constraints**:
- Non-extended roles (assessing, closing, architect) MUST ignore the
  sentinel. Allow-list, not deny-list.
- Implementer single-signal guardrail (FR-070-7) MUST NOT change.
- Reviewer/security/qa `blocked` with a posted PR comment MUST still
  route to `phase=blocked` (US5 regression).

## Files Touched

```text
src/coordinare/state_store.py                       # PersistedSession: head_at_dispatch, head_at_last_turn
src/coordinare/session.py                           # CardSession mirror fields + persist/restore
src/coordinare/graph/nodes/monitor_performer.py     # allow-list gate; per-role guardrail; head_at_dispatch (sticky) + head_at_last_turn writes
src/coordinare/protocol.py                          # bot_pr_comment_delta on ProtocolResponse

agent/performer/src/performer/main.py              # sentinel allow-list; comment-delta capture
agent/performer/src/performer/protocol.py          # bot_pr_comment_delta mirror

tests/integration/test_072_implementer_no_commit_reproduction.py    # FR-072-12
tests/integration/test_072_role_checkpoint_protocol.py              # FR-072-13
tests/integration/test_072_reviewer_blocked_regression.py           # FR-072-14
tests/unit/test_state_store.py                     # head-field round-trip
```

## Constitution Check

- **Backward compat**: New `PersistedSession` fields are optional with
  `None` defaults; v1/v2 snapshots load unchanged.
- **Allow-list discipline**: Single shared constant
  `SENTINEL_ROLES = {"implementing", "reviewing", "security", "qa", "documenting"}`
  in both performer and coordinare — no scattered string lists.
- **Test coverage**: every FR has at least one assertion in an
  integration or unit test.

## Out-of-Scope

- Architect / assessing / closing roles — single-turn, checkpointing
  doesn't apply.
- Historical head-delta list (only current pair persisted).
- CLI surfacing of head deltas — separate spec if needed.
- Migrating implementer guardrail to 3-signal — FR-072-7 explicit no-op.

## Risks

- **False-positive guardrail trip** on reviewer turns where the bot
  posts a comment via a different code path (e.g. a status comment
  not associated with the turn). Mitigation: comment delta is
  measured by author == bot user AND created_at > turn_start, which
  is what the performer already does for partial_progress comments.
- **Snapshot round-trip regression**. Mitigation: explicit v1 + v2
  round-trip unit test.
