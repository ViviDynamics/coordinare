# Phase 1 Data Model: Board-Simulation Benchmark — Phase 1

Two data surfaces: (A) the **run artifact** (persisted output, pydantic → JSON) and
(B) the **fake's internal model** (in-memory, per run). The **contract** surface
(the Protocol) is documented in `contracts/github-service-protocol.md`.

## A. Run artifact (persisted)

Pydantic models in `src/coordinare/bench/artifact.py`, serialized to
`runs/<ts>-<confighash>/run.json` (+ a `raw/` subdir for per-dispatch payloads).
`validate()` re-parses the written JSON before the run is declared complete (FR-011).

### RunArtifact (root)

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` | bump on shape changes |
| `run_id` | `str` | `<ts>-<confighash>` |
| `started_at` / `finished_at` | `datetime` | run bounds |
| `wall_clock_seconds` | `float` | |
| `config_fingerprint` | `{hash: str, source_path: str}` | which config drove the run |
| `approver_policy` | `str` | `"gates_green"` (Phase 1 default) |
| `fixture_manifest` | `str` | what was seeded |
| `cards` | `list[CardOutcome]` | |
| `totals` | `RunTotals` | see below |

### RunTotals

| Field | Type | Notes |
|---|---|---|
| `tokens_processed` | `int \| None` | summed best-effort (may be null if none reported) |
| `cost_usd` | `float \| None` | token×rate estimate |
| `cost_estimated` | `bool` | always `True` this phase (FR-012) |
| `cards_total` | `int` | |
| `cards_merged` | `int` | |
| `cards_terminal_nonmerge` | `int` | blocked + abandoned + error |

### CardOutcome

| Field | Type | Notes |
|---|---|---|
| `card_id` / `issue_ref` / `title` | `str` | |
| `fixture_id` | `str` | links to planted ground-truth (for 135) |
| `final_state` | `Literal["merged","blocked","abandoned","error"]` | FR-009; maps to board reality (D10) |
| `reached_stages` | `list[str]` | canonical order actually traversed |
| `dispatches` | `list[PersonaDispatch]` | |
| `gate_decisions` | `list[GateDecision]` | |
| `ci_results` | `list[CIResult]` | per head_sha |
| `merge` | `{merged: bool, merge_commit: str\|None, approved_by: str\|None}` | |
| `timing` | `{first_dispatch_at, terminal_at, seconds}` | |
| `cost` | `{tokens_processed: int\|None, cost_usd: float\|None, cost_estimated: bool}` | |

### PersonaDispatch

| Field | Type | Notes |
|---|---|---|
| `stage` / `role` / `model` / `backend` | `str` | |
| `job_id` / `session_id` / `container_id` | `str \| None` | from `dispatch_card` ack |
| `status` | `Literal["succeeded","failed","cancelled","error"]` | terminal status from `check_status` |
| `terminal_marker` | `str \| None` | e.g. `"approved"`, `"qa_passed"` |
| `started_at` / `finished_at` / `seconds` | timing | |
| `tokens_processed` | `int \| None` | best-effort aggregate |
| `raw_summary_ref` | `str` | path under `raw/` to full performer summary |

### GateDecision

| Field | Type | Notes |
|---|---|---|
| `stage` | `str` | reviewer / security / qa / ci |
| `verdict` | `Literal["pass","hold","bounce","escalate"]` | as recorded |
| `head_sha` | `str` | |
| `required_checks` / `failed_checks` | `list[str]` | for the CI gate |
| `decided_at` | `datetime` | |

### CIResult

| Field | Type | Notes |
|---|---|---|
| `head_sha` | `str` | |
| `required_checks` | `list[str]` | e.g. `["pytest"]` |
| `conclusion` | `Literal["success","failure"]` | |
| `pytest_exit` | `int` | real exit code |
| `summary_ref` | `str` | path under `raw/` to pytest output |

## B. Fake internal model (in-memory, per run)

`FakeGitHubService` — a **plain mutable object** (D3: a node writes
`_pr_checks_service_cache` onto it). Holds:

| Component | Shape | Behavior |
|---|---|---|
| **Board** | `cards: dict[item_id → {status, title, body, issue_id, pr_id}]` | `poll_board()` → real snapshot shape (`{"snapshot": {column: [item_id,…]}, …}`); `move_card(item_id, status)` mutates the column |
| **Repos** | `bare_repo_path` per fixture (`file://`) + working checkouts | git-reading methods run real `git` against it |
| **PRs** | `prs: dict[pr_id → {head_ref, base_ref, head_sha(live from git), reviews:[…]}]` | `find_pr_for_issue`, `check_mergeability`, `branch_*`, `list_prs_by_branch_prefix` |
| **Reviews** | `list[review_dict]` per PR | approver inserts APPROVED review from a `human_reviewers` login when policy fires |
| **CI states** | `cache: dict[head_sha → CheckRollup]` | computed by running real pytest against the head checkout |
| **Comments / labels / merges** | recorded in-memory | `squash_merge` performs a **real local git merge** into the default branch |
| **Event log** | `list[dict]` — one record per mutating call | the artifact's raw material (D5) |
| `approver` | `Callable[[pr_state], bool]` | default `gates_green` |
| `human_reviewers` | `list[str]` | the login the approver uses |

**Invariant (FR / D1)**: every method degrades to a safe default and **never
raises** — a raise would abort `daemon.start()`.

## Validation rules (from requirements)

- **FR-009 / D10**: every `CardOutcome.final_state` ∈
  `{merged, blocked, abandoned, error}` — no card left without a terminal state.
- **FR-011**: `RunArtifact` must re-parse from its written JSON (round-trip) before
  the run is declared complete.
- **FR-012**: `cost_estimated` is `True` and `cost_usd` is derived from
  `tokens_processed × rate`; both may be `None` if no tokens were reported.
- **D3 shape fidelity**: `check_mergeability` returns the 6-key dict; `get_pr_reviews`
  returns dicts with keys `id, author_login, state, body, submitted_at, comments,
  commit_oid`; `poll_board` returns the `snapshot` shape — verified against the real
  service in the conformance + fake-behavior tests.
