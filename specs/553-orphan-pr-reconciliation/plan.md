# Plan: Orphaned PR reconciliation for completed cards

**Spec**: [spec.md](spec.md) · **Issue**: #78
**Live acceptance fixtures**: conductor-e2e-sample PR21 (superseded by PR22) and PR3 (duplicate of PR2)

## Scope

In:
- Completion-time reconciliation: after a card's PR merges and the card completes, close every other open PR attributable to that card with an explanatory comment.
- Per-cycle janitor sweep: close open PRs whose resolved card is terminal (DONE/CLOSED), with the same comment shape.
- Attribution by card identity (issue linkage), covering legacy branch prefixes.
- `orphan_pr:` config block, default off, independent switches for the two behaviors (090 opt-in pattern).
- Structured-log decision vocabulary per evaluated PR.

Out:
- Branch deletion of dangling card branches (separate concern; branches may be needed by future scenarios).
- Changes to the rebase machinery (bug #76 tracks its own fix).
- Any autonomous reopen/re-dispatch of completed cards.

## Assumptions

- Card identity is best resolved via the issue a card tracks: PRs reference the issue (body/branch), and the board maps card ↔ issue. The `coordinare/` prefix is an optimization, never the sole basis.
- The comment is posted by the daemon identity with the standard `coordinare-attribution` marker (existing lifecycle-comment convention).
- GitHub surface needs a new "list open PRs for the target repo" method next to the existing `list_prs_by_branch_prefix`, and a close-with-comment capability that the closer path does not already have under that shape.
- The sweep runs inside the existing check_board node, end-of-cycle, after the board read and after the startup reconciliation pass, so it sees fresh board state.
- Disablement must be observable: when off, the code path exits before any GitHub read for reconciliation purposes (byte-identical behavior, SC-003).

## Technical approach

- New module `src/coordinare/services/orphan_pr.py`:
  - `resolve_pr_to_card(...)` — attribution: (1) known session workspace branch, (2) issue-number linkage from PR body/branch, (3) canonical + legacy branch prefixes. Returns the card plus a resolution-basis string; `None` when unresolvable (skip, never close).
  - `close_superseded_prs(...)` — completion-time entry point, called from the merge path with the just-completed card and the superseding PR identity.
  - `run_orphan_pr_sweep(...)` — janitor entry point, called from check_board end-of-cycle when enabled.
  - Comment assembly with the attribution marker, card reference, terminal state, superseding PR link, and reason.
- `GitHubService` additions (protocol + real + fake): `list_open_pull_requests()` and `close_pull_request_with_comment(...)`; fake gets fixture-friendly behavior for tests.
- Config: `OrphanPrConfig` on the symphony-level config (`enabled`, `completion_reconciliation`, `sweep`, all default false), validated like the 090 gates.
- Log vocabulary: `orphan_pr.decision` with `outcome=closed|skipped-active|skipped-unresolved|disabled`, `basis=`, `pr=`, `superseded_by=` (mirroring the 076 reconciliation vocabulary style).

## Tasks

See [tasks.md](tasks.md). Strict TDD per task; test file `tests/unit/test_553_orphan_pr_reconciliation.py`.
