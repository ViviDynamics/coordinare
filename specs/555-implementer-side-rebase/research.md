# Research: Implementer-Side Rebase

## R1. How the implementer brings the branch up to date

- **Decision** (owner, 2026-10-13): a true rebase. The performer runs `git rebase origin/<base>` when it prepares the workspace; a conflict is aborted and left to the agent, whose instruction is the same relay-feedback entry. At push time the rewritten branch is published with `--force-with-lease=<branch>:<expected_head_sha>`.
- **Why a lease is needed**: every performer write goes through `_push_head_without_clobbering` (`agent/performer/src/performer/workspace.py`, spec 165 FR-013/FR-015), which never force-pushes over an existing branch because the implementer and the documenter side run can both commit to one branch. A rebased branch is not a fast-forward of its remote, so the ordinary path would replay the rebased commits onto the old branch.
- **How 165 is preserved**: `_push_requested_rebase` is reached only when the remote head is not an ancestor of HEAD, and forces only when the dispatch carries the daemon's request, the role is the implementer, no rebase is in progress, the tracked tree is clean, the `main_sha` the daemon saw is an ancestor of HEAD, and the fetched remote head equals `expected_head_sha`. The lease then repeats that last check atomically on the server. A concurrent push makes it fail (`push_branch.rebase_lease_stale` or `push_branch.rebase_lease_rejected`); nothing is overwritten, the turn reports the failure, and because the branch head moved the daemon's marker no longer matches and it classifies again. The daemon also issues no request while a documenter side run is active.
- **Residual race**: a documenter side run that cloned before the rebase and pushes after it goes down the ordinary no-force path and may fail with `push_branch.rebase_conflict`. It loses its own push, never anyone else's commits.
- **A merge is still accepted**: an agent that merges main instead leaves the remote head as an ancestor, so the plain fast-forward push applies.
- **Alternative considered**: instruct a merge only (no history rewrite, no lease). Simpler and was the first implementation; replaced on the owner's decision.

## R2. Default of the new key

- **Decision** (owner, 2026-10-13): `implementer_rebase.enabled` defaults to `true`.
- **Rationale**: the key replaces behavior that 047/096/097 ran unconditionally, so default-on is the closest to the previous build; and the guard fails open, so it cannot wedge dispatch. The key still gives the incident its missing switch.
- **Note**: this departs from the house style of opt-in keys (553 `orphan_pr`, 090 gates). A config object without the attribute reads as the model default.

## R3. What "fail open" means per branch of the guard

| Condition | Before (097) | After |
| --- | --- | --- |
| feature switched off | n/a (no switch) | dispatch |
| no open PR, no github service, no config | dispatch | dispatch |
| pending feedback for the stage | dispatch | dispatch |
| main unknown and the API cannot say | dispatch | dispatch |
| documenter side run active | n/a | dispatch, no request |
| `check_mergeability` raises | dispatch | dispatch, `dispatch_performer.pre_dispatch_guard_failed` |
| mergeability UNKNOWN or head empty | **hold** | dispatch, `dispatch_performer.pre_dispatch_mergeability_unknown` |
| branch not `coordinare/` prefixed | **hold** + FAILED marker | dispatch, `dispatch_performer.pre_dispatch_branch_unknown` |
| PR not OPEN or state unreadable | hold (DEFERRED) | dispatch, no request |
| daemon rebase fails | **hold** (the incident) | path removed |
| diverged, stage is implementer | hold, rebase | dispatch now with the instruction |
| diverged, other stage | hold, rebase | reroute, dispatch next cycle |
| diverged, already requested for same (main, head) | silent hold | card blocked with a reason (FR-013) |

## R4. Anti-thrash marker

- **Decision**: reuse `last_rebase_attempt`. A request writes `{main_sha, head_sha, outcome: "delegated"}`. `should_attempt_rebase` treats `delegated` like `blocked` and `failed`: no second request for the same pair.
- **Rationale**: no schema change (`_persist_last_rebase_attempt` already accepts any non-empty outcome string). Old markers keep their meaning.
- **Escalation** (owner, 2026-10-13): the hold sets `phase="blocked"` with an `open_questions` reason naming the branch, the main revision and the head. The existing `handle_blocked` node moves the card and notifies. A human who moves the branch or leaves an instruction re-enables dispatch.

## R5. Edge round and proactive sweep

- **Decision**: one read-only sweep per board check replaces both. Main movement is still detected and recorded; it no longer triggers anything by itself.
- **Rationale**: the edge round relied on git to find out which branches were stale. Without git the only signal is the platform's mergeability, which is exactly what the 096 proactive sweep read.
- **Consequence**: a branch that is behind main, not conflicting, in a repository that does not require up-to-date branches is no longer touched. 047 rebased it on every main move.

## R6. Audit of other daemon-side code-touching paths (FR-012)

Recorded in `quickstart.md` under "Audit" and repeated in the PR. One item is fixed here: the daemon's `git ls-remote` is replaced by `GitHubService.get_branch_head_sha` (REST `GET git/ref/heads/<branch>`), so the divergence service runs no subprocess. The remaining items are tracked as issues.
