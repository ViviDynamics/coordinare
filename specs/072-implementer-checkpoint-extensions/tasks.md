# Tasks: Checkpoint Protocol Extensions + Head-Delta Audit Trail

**Branch**: `072-implementer-checkpoint-extensions`
**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase A — Protocol extension (FR-072-1..4)

- [ ] T001 Define `SENTINEL_ROLES = frozenset({"implementing", "reviewing", "security", "qa", "documenting"})` in `agent/performer/src/performer/main.py`; replace `role == "implementing"` gate at sentinel parse with `role in SENTINEL_ROLES`.
- [ ] T002 Mirror the constant in `src/coordinare/graph/nodes/monitor_performer.py`; replace `stage == "implementing"` gate on the `partial_progress` branch with `stage in SENTINEL_ROLES` and ensure `performer_stage` is preserved verbatim (no coercion to `implementing`).
- [ ] T003 Confirm performer's "push uncommitted + post PR comment" path is a no-op when there is no diff (review-only roles); add unit test in `agent/performer/tests/unit/test_main.py` exercising reviewer sentinel.

## Phase B — Per-role zero-progress guardrail (FR-072-5..7)

- [ ] T004 Add `bot_pr_comment_delta: int = 0` to `ProtocolResponse` in `src/coordinare/protocol.py` and `agent/performer/src/performer/protocol.py`.
- [ ] T005 Performer captures pre-turn and post-turn bot-authored comment count and emits the delta on every terminal `ProtocolResponse` (`agent/performer/src/performer/main.py`).
- [ ] T006 In `monitor_performer.py`, add per-role guardrail block: when `stage in {"reviewing","security","qa","documenting"}` AND `head_before == head_after` AND `bot_pr_comment_delta == 0` AND no new clarifications appended this turn → route to `phase=dispatching` with role-appropriate `relay_feedback` ("resume your review", etc.).
- [ ] T007 Verify FR-070-7 single-signal guardrail for `implementing` is untouched (separate code path / explicit comment).

## Phase C — Head-delta audit trail (FR-072-8..11)

- [ ] T008 Add `head_at_dispatch: str | None = None` and `head_at_last_turn: str | None = None` to `PersistedSession` in `src/coordinare/state_store.py`.
- [ ] T009 Add matching fields on `CardSession` in `src/coordinare/session.py`; persist/restore through `_session_to_persisted` and `_persisted_to_session` helpers.
- [ ] T010 In `monitor_performer.py`, write `state["head_at_dispatch"] = head_before` only when currently unset / falsy (sticky: capture first non-empty `head_before` seen for this card's session, then never overwrite).
- [ ] T011 In `monitor_performer.py`, overwrite `session.head_at_last_turn = response.head_after` whenever `head_after` is non-null on terminal responses.
- [ ] T012 Mirror both fields into top-level snapshot state for single-card legacy paths, matching `last_blocked_notified_at`.
- [ ] T013 Unit test: v1 snapshot (no head fields) loads and round-trips with `None` values; v2 snapshot round-trips populated values.

## Phase D — Integration tests (FR-072-12..14)

- [ ] T014 `tests/integration/test_072_implementer_no_commit_reproduction.py` — drive workflow against a fake performer returning `{status: "blocked", head_before: "X", head_after: "X"}`; assert `phase=dispatching` and `relay_feedback` populated; phase never `blocked`.
- [ ] T015 `tests/integration/test_072_role_checkpoint_protocol.py` — multi-turn workflow: reviewer emits `partial_progress` (re-dispatched to `reviewing`, stage preserved); then qa emits `blocked` with `head_before==head_after` AND zero bot comments AND zero new clarifications → re-dispatched.
- [ ] T016 `tests/integration/test_072_reviewer_blocked_regression.py` — reviewer returns `blocked` with `bot_pr_comment_delta=1` → routes to `phase=blocked`; implementer commits + blocked → routes to `phase=blocked`.

## Phase E — Verification

- [ ] T017 Run `.venv/bin/pytest tests/` from repo root — full coordinare suite green.
- [ ] T018 Run `cd agent/performer && .venv/bin/pytest tests/unit/` — performer suite green.
- [ ] T019 Run `.venv/bin/ruff check src/ tests/` — clean.
- [ ] T020 Commit, push, open PR targeting `main`.
