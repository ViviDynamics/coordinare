# Phase 0 Research: Board-Simulation Benchmark — Phase 1

All findings below were verified against the codebase during planning (file:line
refs are current as of 2026-07-23). This document resolves every unknown; no
`NEEDS CLARIFICATION` markers remain.

## D1. Injection seam — inject a fake at `state["github_service"]`, bypass `__main__`

- **Decision**: Construct the daemon in a harness and set
  `daemon.state["github_service"] = FakeGitHubService(...)` post-construction; do
  not thread a flag through `__main__`.
- **Rationale**: The daemon's only contact with the outside world is
  `GitHubService` (`src/coordinare/services/github.py:449`). Nodes never construct
  it — they read `state.get("github_service")` fresh each cycle. The exact pattern
  is already used by tests (`tests/unit/test_daemon_restart_reconcile.py:70`:
  `daemon._state["github_service"] = fake`). Construction sites are only in
  `__main__.py:723` (single) and `:979` (multi-symphony).
- **Gotchas** (verified): (a) keep `state["symphony_configs"]` empty or the
  per-cycle symphony swap (`daemon.py:1819`) overwrites the injected service
  mid-loop; (b) an exception raised inside a node aborts `daemon.start()`
  (`daemon.py:3249`) rather than looping — the fake must never raise.
- **Alternatives rejected**: threading a `--fake-github` flag through `__main__`
  (couples production entrypoint to test scaffolding); subclassing the real service
  (drags real HTTP/GraphQL internals).

## D2. Daemon knobs — everything the harness needs already exists

- **Decision**: `CoordinareDaemon(CoordinareGraphBuilder().build(),
  poll_interval_seconds=1, max_cycles=N, sleep_func=<no-op>, state_store=None)`,
  then `daemon.state.update({...fakes...})`, then `await daemon.start()`.
- **Rationale** (verified at `daemon.py:605`): `max_cycles`, `sleep_func`
  (injectable — tests pass a no-op), `poll_interval_seconds`, and
  `state_store=None` (disables persistence entirely — every save/load is guarded)
  are all constructor params. Loop is `start()` (`daemon.py:2717`); terminates on
  `max_cycles`, `stop()`, or cancellation. `CoordinareGraphBuilder().build()`
  (`builder.py:54`) also accepts `node_overrides` — the seam used to stub the
  performer in CI.
- **Alternatives rejected**: real 30s polls (too slow — inject `sleep_func`);
  throwaway state-store path (unnecessary — `None` disables it).

## D3. The Protocol is undersized — complete it to the node-called surface

- **Decision**: Expand `GitHubServiceProtocol` (`graph/state.py:18`, currently 7
  methods) to the ~18 methods nodes actually call, plus the private
  `_current_token`, plus documenting the attribute reads (`_org`, `_project_name`,
  `project_id`) and the attribute *write* (`_pr_checks_service_cache`). Add a
  conformance test asserting the real `GitHubService` still satisfies it.
- **Node-called surface** (verified by node→method mapping):
  `poll_board`, `move_card`, `get_issue_details`, `get_issue_comments`,
  `find_pr_for_issue`, `count_closed_prs_for_issue`, `get_pr_reviews`,
  `get_pr_review_context`, `request_reviews`, `request_reviewers`,
  `check_mergeability`, `squash_merge`, `add_comment`, `get_pr_diff`,
  `compare_changed_files`, `get_file_blob_sha`, `get_required_status_checks`,
  `fetch_failed_job_log`; dynamic via `getattr`/`hasattr`: `_current_token`; and the
  latent `post_comment` call (see D6).
- **Rationale**: The real class duck-types the Protocol already, so completing it is
  pure interface capture (no behavior change). Covering only what nodes call keeps
  the contract honest (not all 34 public methods).
- **Consequence**: the fake must be a **plain mutable object** (no `__slots__`, no
  frozen model) because a node *writes* `_pr_checks_service_cache` onto it.

## D4. CI shapes come from `PrChecksService`, not `GitHubService`

- **Decision**: Feed CI by pre-populating
  `github._pr_checks_service_cache[(owner, repo)]` with a fake `PrChecksService`
  that returns a `CheckRollup` built from **real pytest** against the PR-head
  checkout. Also implement `get_required_status_checks(...) -> set[str] | None`
  (return `{"pytest"}`) and `fetch_failed_job_log(...) -> str` (pytest tail or `""`).
- **Rationale** (verified): `CheckEntry`/`CheckRollup`
  (`pr_checks_service.py:38,56`) are produced by `PrChecksService`, which issues its
  own GraphQL via `github._execute(...)`. The consuming node reads/writes the
  `_pr_checks_service_cache` at `monitor_performer.py:2297-2306`. Pre-populating that
  cache is the clean seam — no GraphQL emulation. `get_required_status_checks`
  returns `set[str] | None` (`github.py:1784`); `None` means "branch protection
  unreadable" and triggers a fallback path — the fake returns a real set.
- **Alternatives rejected**: implementing `github._execute()` to pattern-match the
  `statusCheckRollup` GraphQL query (fragile string emulation).

## D5. No in-process event bus — build the artifact from the fake's own log

- **Decision**: Reconstruct the artifact from (a) the fake's recorded log of every
  mutating API call (board moves, PR creates, reviews, CI runs, merges) and (b) a
  thin recording wrapper around the performer service for per-dispatch data. Do not
  parse structlog.
- **Rationale** (verified): the daemon has no pub/sub; it writes structlog JSON to
  stderr (`__init__.py:24-58`). But the harness runs the daemon **in process**, and
  the fake already sees every board/PR/review/CI/merge mutation — that *is* the run's
  event stream. Per-dispatch info (`session_id`, `job_id`, `container_id`, status,
  `tokens_processed`, timing) lives on the performer service's `dispatch_card` /
  `check_status` returns (`http_performer_service.py:256,556`), captured by wrapping
  the injected service.
- **Alternatives rejected**: `COORDINARE_OUTPUT_MODE=structured` + stderr parsing
  (couples the artifact to log formatting).

## D6. Latent `post_comment` bug — the fake absorbs it

- **Decision**: The fake implements `post_comment(issue_number, body)` (records +
  no-ops) so the branch is captured rather than silently `AttributeError`'d.
- **Rationale** (verified): `monitor_performer.py:4244` calls
  `github.post_comment(int(issue_number), ...)`, which does not exist on the real
  `GitHubService` (the real method is `add_comment(subject_id: str, body: str)`);
  the call is swallowed by a broad `try/except`. Not fixing production here (out of
  scope); the fake just tolerates and records it.

## D7. Completion — a single injected callable, default `gates_green`

- **Decision**: The fake takes one callable `approver(pr_state) -> bool`, default
  `gates_green`: approve once coordinare's own gate markers (reviewer/qa/security) +
  CI are green, then insert an APPROVED review from a `human_reviewers` login so
  `check_mergeability` sees `review_decision == "APPROVED"`.
- **Rationale** (verified): `check_mergeability`
  (`github.py:1684`) computes `mergeable = (mergeable_raw == "MERGEABLE") AND
  (review_decision == "APPROVED")`; `classify_reviewer` (`models/review.py:70`)
  treats a login in `human_reviewers` as `HUMAN`. A callable is the whole pluggable
  seam — Phase 2's oracle approver drops in without a Protocol+class hierarchy for a
  single implementation.
- **Alternatives rejected**: an `ApproverPolicy` Protocol + `GatesGreenApprover`
  class now (speculative abstraction for one implementation).

## D8. Cost — local per-card estimate (USER DECISION)

- **Decision**: Fill artifact cost from the per-card estimate coordinare already
  computes: `tokens_processed × cost_per_million_tokens`
  (`monitor_performer.py:3279-3290`, rate from `CostTrackingConfig`,
  `config.py:226-230`). Flag it `cost_estimated: True`. Authoritative proxy USD is
  deferred to a later phase.
- **Rationale**: The performer summary carries **no** cost and no prompt/completion
  split — only a best-effort aggregate `tokens_processed`
  (`protocol.py:85`, often present only on `working` responses). Real LiteLLM cost
  would require new tag-injection plumbing in *every backend's hot path*
  (claude_code shim headers + `opencode.json`) plus scrubber carve-outs
  (`translate/request.py:27`, `opencode_compat.py:73`), against an external
  operator-run gateway (`litellm.vividynamics.com`) whose spend-logging config is
  not verifiable from this repo — cross-cutting work outside a "fake the GitHub API"
  substrate spec.
- **Alternatives rejected**: (a) whole-run total via LiteLLM `/spend/logs`
  (real USD but no per-dispatch attribution + unconfirmable external dependency);
  (b) full per-dispatch attribution (large cross-cutting change; 135+ scope).
- **Upgrade path**: `# ponytail: token×rate estimate, not proxy USD; wire LiteLLM
  /spend/logs when a later phase needs authoritative cost.`

## D9. CI integration test — stubbed performer (USER DECISION)

- **Decision**: The one automated end-to-end test stubs the performer via
  `node_overrides` (canned success) so the loop is deterministic and free.
  Real-performer runs are opt-in via `scripts/board_bench.py`.
- **Rationale**: Real models are nondeterministic and cost money — a stub keeps unit
  CI fast, reliable, and free while still proving the loop closes and the artifact
  validates (SC-006). Constitution II requires deterministic tests. Full-fidelity
  quality signal (the reason performers are real) is exercised by the opt-in CLI,
  not unit CI.
- **Alternatives rejected**: a real cheap model in CI (nondeterministic, flaky,
  costs tokens).

## D10. Terminal-state detection — board columns + hard budget

- **Decision**: A card is terminal when it lands in `DONE` (merged) or `BLOCKED`;
  the run's hard stop is `max_cycles` + wall-clock. Cards that never reach a terminal
  column before budget exhaustion → `abandoned`; unhandled error → `error`.
- **Rationale** (verified): card columns are `TODO/BLOCKED/IN_PROGRESS/IN_REVIEW/DONE`
  (`models/card.py:10`); `merge_pr` moves to `DONE` (`merge_pr.py:73`),
  `handle_blocked` to `BLOCKED` (`handle_blocked.py:165`). No separate
  `escalated`/`abandoned` column exists — escalation also lands in `BLOCKED`. Note:
  `BLOCKED` is not permanently terminal (spec-129 env-recovery can re-surface it), so
  the hard budget is the authoritative stop, with "all cards DONE|BLOCKED" as the
  happy-path early exit.
