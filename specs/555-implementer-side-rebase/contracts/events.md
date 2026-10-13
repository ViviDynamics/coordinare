# Contract: structured log events

## Unchanged

| Event | Emitted by | Note |
| --- | --- | --- |
| `fetch_main_sha.failed` | daemon | now a REST read of the branch head, not `ls-remote` |
| `rebase.skipped_active_performer` | daemon | |
| `rebase.pr_state_unreadable`, `rebase.pr_state_deferred` | daemon | now guards a request, not a branch write |
| `rebase.triggered` | daemon | `reason` is `pre_dispatch` or `proactive_conflict`; `outcome` is now always `delegated` |
| `rebase.performer_dispatched` | daemon | emitted when the card is routed to the implementer |
| `check_board.main_head_changed`, `multi_session.preflight.main_head_changed` | daemon | main is recorded; nothing is rebased |
| `check_board.proactive_mergeability_failed`, `check_board.proactive_rebase_failed` | daemon | per card |
| `check_board.rebase_round_failed` | daemon | now wraps the whole read-only sweep |
| `dispatch_performer.pre_dispatch_guard_failed` | daemon | fail open |
| `dispatch_performer.pre_dispatch_held_on_conflict` | daemon | anti-thrash; gains `escalated=true` and the card is blocked |
| `dispatch_performer.pre_dispatch_branch_unknown` | daemon | now followed by a normal dispatch |

## New

| Event | Meaning |
| --- | --- |
| `dispatch_performer.pre_dispatch_mergeability_unknown` | mergeability not computed; dispatching anyway |
| `dispatch_performer.pre_dispatch_rerouted_to_implementer` | another stage was due; the implementer goes first, next cycle |
| `dispatch_performer.pre_dispatch_side_writer_active` | a documenter side run is active; dispatching without a request |
| `workspace.rebase.clean` (performer) | the setup rebase onto the base succeeded |
| `workspace.rebase.conflict` (performer) | the setup rebase stopped on a conflict and was aborted for the agent |
| `workspace.rebase.skipped_head_moved` (performer) | the branch moved after the request; history left alone |
| `workspace.rebase.failed` (performer) | the setup rebase could not run; setup continues |
| `push_branch.rebased_onto_base` (performer) | the rebased branch was published under the lease |
| `push_branch.rebase_lease_stale`, `push_branch.rebase_lease_rejected` (performer) | someone else pushed; nothing was overwritten |

## Removed from the daemon, and where the work now reports

| Old daemon event | New owner and event |
| --- | --- |
| `rebase.round_starting`, `rebase.round_complete`, `rebase.no_stale_branches` | none; there is no round. `rebase.triggered` marks each request |
| `rebase.clean` | performer: `workspace.rebase.clean`, then `push_branch.rebased_onto_base` |
| `rebase.conflict_detected` | performer: `workspace.rebase.conflict` |
| `rebase.failed` | performer: `workspace.rebase.failed`, or the turn's own failure report |
| `rebase.push_failed` | performer: `push_branch.rebase_lease_stale`, `push_branch.rebase_lease_rejected`, `push_branch.rebase_in_progress`, `push_branch.uncommitted_changes` |
| `rebase.already_up_to_date` | none; a current branch classifies as `proceed` silently |
| `rebase.notification_failed` | none; the round summary notification is gone |
| `dispatch_performer.pre_dispatch_defer` | `dispatch_performer.pre_dispatch_mergeability_unknown` (and it no longer defers) |
| `dispatch_performer.pre_dispatch_rebase_no_op` | none |
| `merge_pr.rebase_round_complete`, `merge_pr.rebase_round_failed` | none |
| `multi_session.preflight.rebase_round_failed` | none |
| `check_board.edge_rebase_postprocess_failed` | `check_board.proactive_rebase_failed` (per card) |

An operator alert keyed on `rebase.failed` will go quiet after this change. That is the intended outcome, not a lost signal.
