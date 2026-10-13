# Contract: structured log events

## Unchanged

| Event | Emitted by | Note |
| --- | --- | --- |
| `fetch_main_sha.failed` | daemon | read-only `ls-remote` |
| `rebase.skipped_active_performer` | daemon | |
| `rebase.pr_state_unreadable`, `rebase.pr_state_deferred` | daemon | now guards a request, not a branch write |
| `rebase.triggered` | daemon | `reason` is `pre_dispatch` or `proactive_conflict`; `outcome` is now always `delegated` |
| `rebase.performer_dispatched` | daemon | emitted when the card is routed to the implementer |
| `check_board.main_head_changed`, `multi_session.preflight.main_head_changed` | daemon | main is recorded; nothing is rebased |
| `check_board.proactive_mergeability_failed`, `check_board.proactive_rebase_failed` | daemon | per card |
| `check_board.rebase_round_failed` | daemon | now wraps the whole read-only sweep |
| `dispatch_performer.pre_dispatch_guard_failed` | daemon | fail open |
| `dispatch_performer.pre_dispatch_held_on_conflict` | daemon | anti-thrash hold |
| `dispatch_performer.pre_dispatch_branch_unknown` | daemon | now followed by a normal dispatch |

## New

| Event | Meaning |
| --- | --- |
| `dispatch_performer.pre_dispatch_mergeability_unknown` | mergeability not computed; dispatching anyway |
| `dispatch_performer.pre_dispatch_rerouted_to_implementer` | another stage was due; the implementer goes first, next cycle |

## Removed from the daemon, and where the work now reports

| Old daemon event | New owner and event |
| --- | --- |
| `rebase.round_starting`, `rebase.round_complete`, `rebase.no_stale_branches` | none; there is no round. `rebase.triggered` marks each request |
| `rebase.clean` | performer: the implementer's normal turn completion; the push logs `push_branch.rebased` only when it had to replay onto concurrent remote work |
| `rebase.conflict_detected` | daemon `rebase.triggered` (the platform's CONFLICTING verdict); resolution happens in the implementer's turn |
| `rebase.failed`, `rebase.push_failed` | performer: `push_branch.rebase_conflict`, `push_branch.uncommitted_changes`, or the turn's own failure report |
| `rebase.already_up_to_date` | none; a current branch classifies as `proceed` silently |
| `rebase.notification_failed` | none; the round summary notification is gone |
| `dispatch_performer.pre_dispatch_defer` | `dispatch_performer.pre_dispatch_mergeability_unknown` (and it no longer defers) |
| `dispatch_performer.pre_dispatch_rebase_no_op` | none |
| `merge_pr.rebase_round_complete`, `merge_pr.rebase_round_failed` | none |
| `multi_session.preflight.rebase_round_failed` | none |
| `check_board.edge_rebase_postprocess_failed` | `check_board.proactive_rebase_failed` (per card) |

An operator alert keyed on `rebase.failed` will go quiet after this change. That is the intended outcome, not a lost signal.
