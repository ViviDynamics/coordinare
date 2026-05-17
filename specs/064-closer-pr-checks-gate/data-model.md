# Data Model: Closer PR Checks Gate (064)

All models live in `src/coordinare/services/pr_checks_service.py` (I/O types)
and `src/coordinare/services/pr_checks_policy.py` (decision type). Pydantic v2
`BaseModel` throughout.

## CheckEntry

One row of the GitHub `statusCheckRollup` — represents a single check-run or
commit-status leg.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Display name (e.g. `"ci/test"`, `"build"`). |
| `status` | `Literal["queued", "in_progress", "completed"]` | GitHub lifecycle state. |
| `conclusion` | `Literal["success","failure","neutral","cancelled","skipped","timed_out","action_required","stale","startup_failure"] \| None` | `None` while `status != "completed"`. |
| `is_required` | `bool` | Whether the check appears in the target branch's required-checks set. Defaults `False` when branch protection is unreadable. |
| `details_url` | `str \| None` | Link surfaced in operator logs and closer artifacts. |

Validation rules:
- `conclusion` must be `None` iff `status != "completed"`.
- `name` is not unique on its own; `(name, head_sha)` is unique per rollup.

## CheckRollup

Aggregate snapshot for one PR HEAD commit. Returned by
`PrChecksService.get_pr_check_rollup(pr_number)`.

| Field | Type | Notes |
|---|---|---|
| `pr_number` | `int` | |
| `head_sha` | `str` | The PR HEAD at query time; used to detect HEAD drift between gate evaluations. |
| `head_pushed_at` | `datetime` | UTC. Anchor for `pending_timeout` (FR-009). |
| `branch_protection_readable` | `bool` | `False` if the token cannot read `branchProtectionRules`; degrades `is_required` to `False` everywhere. |
| `checks` | `list[CheckEntry]` | May be empty (e.g. no workflows configured). |

Derived properties (computed, not stored):
- `all_required_passing` — every entry with `is_required=True` has a passing conclusion under the FR-002 mapping.
- `any_required_failing` — any required entry has a refusing conclusion.
- `any_required_pending` — any required entry is not yet `completed`.

## GateDecision

The pure-function policy output. Consumed by both the closer persona (via
serialized rollup → directive) and coordinare's `_advance_stage`.

| Field | Type | Notes |
|---|---|---|
| `action` | `Literal["FORWARD", "BOUNCE", "HOLD"]` | `FORWARD` → advance to `monitoring_pr`. `BOUNCE` → return card to `implementer` with failed-job list. `HOLD` → stay in current stage; re-poll on next tick. |
| `reason` | `str` | Short human-readable summary written to logs and card state. |
| `failed_jobs` | `list[str]` | Names of refusing required checks. Empty unless `action == "BOUNCE"`. |
| `elapsed_seconds` | `float \| None` | Wall-clock from `head_pushed_at` to now; set on `HOLD` and on `BOUNCE`-due-to-timeout. |

## Conclusion → Decision Mapping (FR-002)

Applies to **required** checks only:

| GitHub `conclusion` | Effect |
|---|---|
| `success` | passing |
| `neutral` | passing |
| `skipped` | passing |
| `failure` | refusing |
| `cancelled` | refusing |
| `timed_out` | refusing |
| `action_required` | refusing |
| `stale` | refusing |
| `startup_failure` | refusing |
| `None` (status ≠ completed) | pending |

Non-required checks never block; they appear in logs but not in `failed_jobs`.

## State Storage

The coordinare adds `checks_state` to the per-card lifecycle entry in
`state.py`:

```python
class CardChecksState(BaseModel):
    head_sha: str
    first_observed_at: datetime    # when this HEAD first entered HOLD
    last_decision: Literal["FORWARD", "BOUNCE", "HOLD"]
    last_polled_at: datetime
```

Reset whenever `head_sha` changes (closer pushed a fix; clock restarts).
