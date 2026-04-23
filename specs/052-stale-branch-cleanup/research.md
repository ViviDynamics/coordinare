# Research: Stale Branch Cleanup

## Decision: GitHub REST API for branch existence check and deletion

- **Decision**: Use GitHub REST (`GET /repos/{owner}/{repo}/branches/{branch}` and `DELETE /repos/{owner}/{repo}/git/refs/heads/{branch}`) rather than GraphQL.
- **Rationale**: GitHub GraphQL has no direct branch-delete mutation. REST `branches/{name}` returns 200/404 cleanly. httpx is already present in the codebase.
- **Alternatives considered**: GraphQL `createRef`/`deleteRef` — complex, branch checks are simpler via REST.

## Decision: Check happens in workspace.prepare(), not dispatch_performer

- **Decision**: Stale branch check lives in `WorkspaceManager.prepare()` alongside the clone/branch-create logic.
- **Rationale**: The branch name is computed in `prepare()` using `make_branch_name()`. All workspace setup logic is already here. Adding it to `dispatch_performer.py` would duplicate the branch-name computation.

## Decision: Non-fatal deletion failure

- **Decision**: If `delete_branch()` fails (e.g., permission error, branch protected), log a warning and continue. Let the performer handle the existing branch gracefully.
- **Rationale**: Stale branch deletion is best-effort. A warning is more useful than a hard failure that blocks dispatch. The performer may still succeed if it can force-push or the branch is compatible.

## Decision: suffix strategy checks up to -9, then falls back to delete

- **Decision**: For `branch_collision_strategy: suffix`, try `-2` through `-9`. If all taken (extremely unlikely), fall back to delete and log a warning.
- **Rationale**: More than 9 stale copies of the same card's branch indicates an operator problem that should be surfaced, not silently extended. Delete gives a clean state.

## Decision: No changes to make_branch_name()

- **Decision**: `make_branch_name()` stays unchanged. The suffix is appended at the workspace level only when a collision is detected.
- **Rationale**: `make_branch_name()` is a pure function used for deterministic naming. Changing it would break existing callers and the "same name on retry" property that makes branch cleanup needed in the first place.
