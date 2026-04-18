# Research: Auto-Rebase Active Branches on Merge

## R1: Merge Detection Strategy

**Decision**: Track `last_known_main_sha` in CoordinareState. After merge_pr succeeds, the new merge commit SHA is immediately available from `github.squash_merge()` return. For non-coordinare merges (human merges a PR), detect the change by comparing `last_known_main_sha` against the current main HEAD at the start of each poll cycle (via `git ls-remote` or the existing GitHub API).

**Rationale**: The coordinare already knows the merge SHA from the squash_merge response — zero extra API calls for coordinare-initiated merges. For external merges, a lightweight `git ls-remote refs/heads/main` check (one HTTP call) per poll cycle detects any change.

**Alternatives considered**:
- GitHub webhook for push events: Requires webhook infrastructure, SSL certificate, public endpoint. Too much setup for a daemon that already polls.
- Check board snapshot for DONE column changes: Doesn't capture non-coordinare merges, and DONE status lags behind the actual merge.

## R2: Rebase Execution Strategy

**Decision**: For each stale branch, clone the repo into a temporary directory, fetch main + the branch, run `git rebase origin/main`, and if clean, force-push with lease. Use the coordinare's existing `_run_git` async subprocess pattern.

**Rationale**: Each rebase needs an isolated working directory. The coordinare doesn't keep persistent checkouts — it creates ephemeral workspaces per operation (same pattern as WorkspaceManager). The git CLI is the most reliable rebase tool (no library binding needed).

**Alternatives considered**:
- GitHub API rebase (update-branch endpoint): Only supports merge, not rebase. Doesn't maintain linear history.
- Reuse the performer's workspace: The performer workspace is torn down after each dispatch. A rebase is a coordinare-side operation, not a performer operation (unless conflicts arise).
- In-place rebase on the workspace branch: Risky if the performer is still running. Isolated temp clone is safer.

## R3: Conflict Resolution Strategy

**Decision**: When `git rebase` exits with conflict status (exit code 1 + conflict markers in working tree), the coordinare:
1. Lists conflicted files via `git diff --name-only --diff-filter=U`
2. Reads conflict markers from each file
3. Dispatches a performer (implementer role) with a special `relay_feedback` payload containing the conflict details and the instruction to resolve
4. The performer reads the files, resolves markers, runs `git add` + `git rebase --continue`
5. If the performer succeeds → force-push with lease
6. If the performer fails (returns error/blocked) → block the card with conflict diagnostics

**Rationale**: Reuses the existing performer dispatch infrastructure (dispatch_performer → monitor_performer). The implementer persona is already capable of reading and editing code. No new performer role or transport needed.

**Alternatives considered**:
- Always block on conflict: Defeats the purpose of autonomous parallel work. Most conflicts are mechanical.
- Automatic resolution via git merge strategies (ours/theirs): Too coarse — doesn't understand semantic intent. The performer can make case-by-case decisions.
- Dedicated "rebase resolver" persona: Unnecessary complexity. The implementer persona with conflict context in relay_feedback is sufficient.

## R4: Active-Session Guard

**Decision**: Before rebasing a branch, check `active_sessions[card_id].phase`. If the phase is `monitoring_performer` (performer is actively running), skip that branch for this rebase round. It will be caught on the next round (or after the performer finishes).

**Rationale**: Rebasing a branch while a performer is pushing to it causes a race condition — the performer's next push would fail because the remote HEAD moved. Waiting one cycle (30s) is cheap and safe.

**Alternatives considered**:
- Lock mechanism (mutex per branch): Over-engineered for a single-daemon system. Phase check is simpler and sufficient.
- Kill the performer and rebase: Destructive. The performer may be minutes into an expensive LLM call.

## R5: Force-Push-with-Lease Implementation

**Decision**: Use `git push --force-with-lease=<branch>:<expected-sha> origin <branch>` where `<expected-sha>` is the HEAD we observed before rebasing. This ensures we don't overwrite a concurrent push from another source.

**Rationale**: `--force-with-lease` is the standard safe force-push. If someone (or a performer) pushed between our rebase and our push, the lease check fails and we retry once.

**Alternatives considered**:
- Bare `--force`: Unsafe — could overwrite concurrent work.
- No force-push (regular push): Won't work after a rebase — the branch history has diverged.

## R6: Slack Summary and Dashboard

**Decision**: Collect all RebaseJob outcomes into a RebaseRound. Post a single Slack notification with the event type `rebase_round_complete`. Add `rebase_status` to the dashboard snapshot: `{outcome, timestamp, new_sha}` per active card.

**Rationale**: One notification per merge event (not per branch) follows the existing pattern from the feedback-cycle-exhausted Slack message. Dashboard extension follows the same `build_snapshot` → SSE pattern used for `blocked_by_dependencies`.

**Alternatives considered**:
- Per-branch Slack messages: Noisy in multi-card mode (N messages per merge).
- No dashboard integration: Defeats the visibility goal — operators need to see rebase status without checking each PR.
