# Research: Implementer-Side Rebase

## R1. How the implementer brings the branch up to date

- **Decision**: the divergence instruction asks the implementer to merge `origin/main` into the branch and commit the merge. It does not ask for a history-rewriting `git rebase`.
- **Rationale**: every performer write goes through `_push_head_without_clobbering` (`agent/performer/src/performer/workspace.py`, spec 165 FR-013/FR-015). When the remote branch exists it never force-pushes: it pushes fast-forward when the remote head is an ancestor (`_push_if_remote_ancestor`, which exists to "preserve resolved merges"), otherwise it rebases the local work onto the remote branch and pushes without force. A branch rebased onto main is not a fast-forward of its remote, so that push path would try to replay the rebased commits onto the old branch and fail with `push_branch.rebase_conflict`. A merge commit is a fast-forward and pushes cleanly. PRs are squash-merged, so main receives the same tree either way.
- **Alternatives considered**: (a) add a force-with-lease path to the performer for a real rebase. Rejected for this change: it reopens the clobbering hazard 165 closed (the implementer and the documenter side run can both commit to one branch). It can be a follow-up if a linear PR history matters. (b) Have the performer run the merge deterministically before the agent turn. Rejected: the task asks for the existing feedback channel, and conflict resolution needs the agent anyway.

## R2. Default of the new key

- **Decision**: `implementer_rebase.enabled` defaults to `false`.
- **Rationale**: house style (553 `orphan_pr`, 090 gates): new keys are opt-in. It also gives the incident its missing switch.
- **Consequence**: because the daemon rebase is removed outright (FR-001), an upgraded deployment gets no automatic branch updates until the key is set. This is not byte-identical to the previous build and cannot be. Called out in the PR and in `quickstart.md`.

## R3. What "fail open" means per branch of the guard

| Condition | Before (097) | After |
| --- | --- | --- |
| feature disabled | n/a (no switch) | dispatch |
| no open PR, no github service, no config | dispatch | dispatch |
| pending feedback for the stage | dispatch | dispatch |
| no repo URL or token, main unknown | dispatch | dispatch |
| `check_mergeability` raises | dispatch | dispatch, `dispatch_performer.pre_dispatch_guard_failed` |
| mergeability UNKNOWN or head empty | **hold** | dispatch, `dispatch_performer.pre_dispatch_mergeability_unknown` |
| branch not `coordinare/` prefixed | **hold** + FAILED marker | dispatch, `dispatch_performer.pre_dispatch_branch_unknown` |
| PR not OPEN or state unreadable | hold (DEFERRED) | dispatch, no request |
| daemon rebase fails | **hold** (the incident) | path removed |
| diverged, stage is implementer | hold, rebase | dispatch now with the instruction |
| diverged, other stage | hold, rebase | reroute, dispatch next cycle |
| diverged, already requested for same (main, head) | hold | hold (unchanged, FR-007) |

## R4. Anti-thrash marker

- **Decision**: reuse `last_rebase_attempt`. A request writes `{main_sha, head_sha, outcome: "delegated"}`. `should_attempt_rebase` treats `delegated` like `blocked` and `failed`: no second request for the same pair.
- **Rationale**: no schema change (`_persist_last_rebase_attempt` already accepts any non-empty outcome string). Old markers keep their meaning.
- **Known limit**: a hold has no escalation of its own, as in 097. It clears when main or the branch moves. Flagged for follow-up rather than widened here.

## R5. Edge round and proactive sweep

- **Decision**: one read-only sweep per board check replaces both. Main movement is still detected and recorded; it no longer triggers anything by itself.
- **Rationale**: the edge round relied on git to find out which branches were stale. Without git the only signal is the platform's mergeability, which is exactly what the 096 proactive sweep read.
- **Consequence**: a branch that is behind main, not conflicting, in a repository that does not require up-to-date branches is no longer touched. 047 rebased it on every main move.

## R6. Audit of other daemon-side code-touching paths (FR-012)

Recorded in `quickstart.md` under "Audit" and repeated in the PR. Nothing in that list is changed by this feature.
