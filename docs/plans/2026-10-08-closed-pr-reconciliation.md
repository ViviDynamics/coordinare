# Externally closed unmerged PR remains in approval monitoring

Issue #551

## Scope
In: reconcile externally CLOSED/unmerged PRs before CI routing, park BLOCKED, release reservations, show an operator reason, persist across restart, resume the same reopened PR explicitly.
Out: automatic reopening, new PR creation, changes to merged PR reconciliation.

## Assumptions
- A human closing a PR is a stop signal. Reopen that PR and move the card to Todo to resume review.
- The existing durable system_error_reason stores the closed-PR reason; no snapshot schema change is needed.

## Tasks
- [x] 1. Query/parse PR state and reconcile CLOSED before CI: failing closed-PR tests (including red CI).
- [x] 2. Preserve the park through blocked handling/restart and implement explicit resume: failing restart, blocked-clarification and reopening tests.
- [x] 3. Run focused regressions, lint and typecheck; hand off for independent review and preflight.

## Verification

Red: closed PRs continued monitoring or bounced to implementation; the query omitted state; nonfocused restart could not resume; closed branches remained rebase candidates.

Green: 305 focused regression tests pass, including 13 issue regressions. `uv run ruff check src tests`, `uv run mypy -p coordinare` (206 modules), and `git diff --check` pass. Independent review and full preflight are handled by the parent agent before shipping.

## Independent review fix

The main-moved rebase runs before monitor_pr observes a human close. A red regression through the real check_board edge rebase reproduced branch mutation and conflict dispatch without a PR-state read. The shared run_rebase_round now reads live PR state before its sole rebase_branch call. CLOSED, unknown, unavailable, or missing lifecycle context produces SKIPPED; OPEN and MERGED retain their existing behavior. Existing clean/conflict/push/notification tests now provide an explicit OPEN PR context so each still exercises its original purpose.

Verification after the fix: 310 focused regressions plus 216 merge, dispatch-rebase, daemon restart/persistence/coverage and webhook tests pass. Ruff src/tests, strict mypy (206 modules), and diff check pass.
