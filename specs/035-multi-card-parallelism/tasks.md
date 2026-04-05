# Tasks: Multi-Card Parallelism (035)

## Phase 1 -- CardSession Abstraction

- [x] Create `src/coordinare/session.py` with `CardSession` TypedDict
- [x] Implement `create_session_from_card()` factory function
- [x] Implement `session_to_state()` — copy session fields into flat CoordinareState
- [x] Implement `state_to_session()` — copy flat state fields into a session
- [x] Define `_SESSION_FIELDS` tuple for round-trip field mapping
- [x] Add `active_sessions: dict[str, Any]` to `CoordinareState` TypedDict
- [x] Add `active_sessions: {}` to `initial_state()`

## Phase 2 -- Config, check_board, Daemon Loop

- [x] Add `max_concurrent_cards: int = Field(default=1, ge=1, le=20)` to `ProjectConfiguration`
- [x] Update `check_board` to pick up multiple TODO cards when `max_concurrent_cards > 1`
- [x] Skip cards already in `active_sessions` (deduplication)
- [x] Respect concurrency limit — stop picking when `len(active_sessions) >= max_concurrent_cards`
- [x] Add `_max_concurrent_cards()` helper to `CoordinareDaemon`
- [x] Add `_invoke_multi_session()` to iterate over active sessions independently
- [x] Copy session -> flat state before graph invocation, flat state -> session after
- [x] Isolate errors per session (one failure does not block others)
- [x] Remove completed sessions (phase=idle, current_card=None) to free capacity

## Phase 3 -- Dashboard and Observability

- [x] Add `coordinare_active_sessions` Gauge to `CoordinareMetrics`
- [x] Update daemon loop to set `active_sessions` gauge after each cycle
- [x] Add `active_session_count` and `active_sessions` list to `DashboardStore.build_snapshot()`
- [x] Each session summary includes card_id, card_title, phase, performer_stage, tokens, cost

## Tests

- [x] `test_session.py` — CardSession creation and factory
- [x] `test_session.py` — session_to_state / state_to_session round-trip
- [x] `test_session.py` — session isolation (two sessions independent)
- [x] `test_session.py` — initial_state includes active_sessions
- [x] `test_session.py` — _SESSION_FIELDS completeness
- [x] `test_multi_card.py` — config default, custom, boundary validation (ge=1, le=20)
- [x] `test_multi_card.py` — check_board single-card mode unchanged (backward compat)
- [x] `test_multi_card.py` — check_board multi-card picks up to limit
- [x] `test_multi_card.py` — check_board under capacity (1 card, limit 3)
- [x] `test_multi_card.py` — check_board skips already-active cards
- [x] `test_multi_card.py` — check_board at capacity (no new cards picked)
- [x] `test_multi_card.py` — check_board deduplication
- [x] `test_multi_card.py` — check_board empty TODO
- [x] `test_multi_card.py` — session fields correct on creation
- [x] `test_multi_card.py` — daemon single-card mode uses direct invoke
- [x] `test_multi_card.py` — daemon multi-session invokes per session
- [x] `test_multi_card.py` — daemon error isolation across sessions
- [x] `test_multi_card.py` — completed sessions removed
- [x] `test_multi_card.py` — no sessions runs graph once (check_board fills)
- [x] `test_multi_card.py` — active_sessions gauge exists in metrics
- [x] `test_multi_card.py` — dashboard includes session count and summaries
- [x] `test_multi_card.py` — dashboard no sessions (empty)
- [x] `test_multi_card.py` — dashboard session summary fields
