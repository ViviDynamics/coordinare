# Contract — Startup Reconciliation Pass

**Module:** `src/coordinare/services/reconciliation.py`
**FRs:** FR-002, FR-003, FR-004, FR-005, FR-008, FR-011, FR-012, FR-013, FR-014
**SCs:** SC-001, SC-002, SC-007

## Public API

```python
async def run_startup_reconciliation(
    state: CoordinareState,
    docker_executor: DockerExecutor,
    *,
    budget_seconds: float = 30.0,
) -> ReconciliationReport: ...

async def handle_potentially_stale_session(
    state: CoordinareState,
    card_id: str,
    *,
    docker_executor: DockerExecutor | None = None,
) -> ReconciliationDecision: ...

def detect_wedged_state(state: CoordinareState) -> WedgeResolution | None: ...
```

## Preconditions

- `state` has been freshly rehydrated from snapshot (for `run_startup_reconciliation`) or is the per-cycle in-process state (for `handle_potentially_stale_session` / `detect_wedged_state`).
- `docker_executor` is a thin wrapper around `docker ps` / `docker stop` / `docker port` subprocess calls. May be `None` for the cycle-time wedge invariant (which doesn't need Docker).

## Postconditions

### `run_startup_reconciliation`
- Returns a `ReconciliationReport` whose `decisions` dict has exactly one entry per card with `phase ∈ {dispatching, monitoring_performer, monitoring_agent}` and non-empty `agent_dispatch.session_id` in the input snapshot.
- For every `ADOPTED` decision: the in-process `_active_jobs[session_id]` is populated; `state.active_sessions[card_id]` is unchanged.
- For every `REAPED_AND_REPLACED` decision: the matching container has been `docker stop`ped; `state.active_sessions[card_id].agent_dispatch` is cleared so the next graph tick fresh-dispatches.
- For every `ORPHAN_SWEPT` container: the container is gone from `docker ps`.
- `wall_clock_seconds` ≤ `budget_seconds` (SC-002).
- `started_at`, `completed_at` are timezone-aware UTC.

### `handle_potentially_stale_session`
- Returns the decision branch taken. Side-effects on `state` mirror the startup pass's decisions for that specific card.

### `detect_wedged_state`
- Returns `None` if the state is not wedged.
- Returns a `WedgeResolution` and applies the resolution side-effect (release / block / op_override). Default is `RELEASED` per FR-020 / clarification Q1.

## Required log events (exact event names; FR-013, SC-007)

| Event name | When | Required fields |
|---|---|---|
| `daemon.reconciliation_pass_started` | Top of `run_startup_reconciliation` | `cards_to_process`, `budget_seconds` |
| `daemon.reconciliation_pass_complete` | End of `run_startup_reconciliation` | `wall_clock_seconds`, `decisions` (dict), `orphans_swept_count` |
| `daemon.reconciliation_pass_aborted_docker_unreachable` | FR-012 trigger | `error` |
| `daemon.reap_failed` | FR-004 reap timeout | `container_id`, `session_id`, `error` |
| `daemon.orphan_swept` | Per orphan stopped | `container_id`, `started_at`, `labels` |
| `check_board.stale_session_reconciled` | Replaces existing `check_board.stale_session_redispatch`; emitted by `handle_potentially_stale_session` | `card_id`, `session_id`, `decision` |
| `daemon.wedged_state_detected` | `detect_wedged_state` finds the forbidden combination | `card_id`, `phase`, `performer_stage`, `active_sessions_keys` |
| `daemon.wedge_resolution` | After `detect_wedged_state` applies the resolution | `card_id`, `resolution` (`released`/`blocked`/`op_override`), `wedge_count_in_window` |

## Performance budget

- p95 wall-clock ≤ 30 s for snapshots with up to 5 in-flight cards (SC-002).
- p95 ≤ 500 ms when snapshot has zero in-flight cards.
- Benchmark in `tests/perf/test_reconciliation_latency.py`.

## Failure modes

| Failure | Behaviour |
|---|---|
| Docker daemon unreachable | Emit `daemon.reconciliation_pass_aborted_docker_unreachable`; refuse to dispatch any ephemeral performer (FR-012); leave dashboard + health endpoints serving so operator can diagnose |
| Single container's job-runner unreachable but Docker is up | That container → `REAPED_AND_REPLACED`; others continue |
| Reap timeout (10 s `docker stop` + `docker kill` window) | Emit `daemon.reap_failed`, mark decision `FRESH_DISPATCHED` anyway, proceed (FR-004 best-effort clause) |
| Budget exceeded mid-pass | Remaining cards left untouched (decision `FRESH_DISPATCHED` for them next cycle); emit `daemon.reconciliation_pass_budget_exceeded` |

## Test coverage

- `tests/unit/services/test_reconciliation.py` — per-function unit tests for `_enumerate_containers`, `_classify_container`, `_adopt`, `_reap`, `detect_wedged_state`.
- `tests/contract/test_reconciliation_log_events.py` — asserts every event in the table above is emitted with required fields when the corresponding branch is taken.
- `tests/integration/test_restart_no_duplicate_dispatch.py` — end-to-end FR-014 regression.
- `tests/integration/test_wedged_state_recovery.py` — FR-020 regression.
- `tests/perf/test_reconciliation_latency.py` — SC-002 budget.
