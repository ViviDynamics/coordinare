# Research: Agent Workspace Management (011)

**Date**: 2026-03-03
**Branch**: `011-agent-workspace`

---

## Decision 1: Git Credential Injection Method

**Decision**: Embed the GitHub token as `x-access-token:{token}` in the HTTPS URL for both the clone step and the subsequent `git remote set-url` call. The authenticated URL is used for `git clone --depth=1` (required for private repos) and is retained in workspace-local `.git/config` via `git remote set-url` for push authentication.

**Rationale**: The workspace is ephemeral — it is removed within 5 seconds of session end. The token lives only in the per-workspace `.git/config`, which is a temp directory owned by the coordinare process user. Using the authenticated URL at clone time is required for private GitHub repositories; `GIT_TERMINAL_PROMPT=0` alone would cause clone to fail without prompting. The `SecretStr` type on the config field ensures the token is redacted from structlog output; the `_run_git` helper must log stderr only, not command arguments, to prevent token leakage to logs.

**Alternatives considered**:
- `GIT_ASKPASS` + `GIT_PAT` env var: Cleanest from a "token never on disk" perspective. GitHub Actions uses this approach. Rejected for now because: the ASKPASS script must persist for the entire performer session (not just during clone), requiring careful lifecycle management of the temp file across clone → dispatch → push. A future security enhancement can adopt this.
- `credential.helper store`: Writes to `~/.git-credentials` on the host — persists across workspaces and users. Rejected.
- Inline `git -c credential.helper='!echo ...'`: Equivalent to ASKPASS but slightly cleaner. Deferred with ASKPASS.

**Implementation**: Use the authenticated URL for both clone and push:
```
clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
git clone --depth=1 {clone_url} {clone_dir}   # auth required for private repos
git remote set-url origin {clone_url}           # retains auth for push
```
`repo_url` (plain HTTPS, no token) is stored in `WorkspaceInfo` and sent in the dispatch payload — the token is never exposed to the performer. `GIT_TERMINAL_PROMPT=0` prevents interactive prompts if auth somehow fails. Token comes from `config.github_token.get_secret_value()`.

---

## Decision 2: Branch Naming Algorithm

**Decision**: `coordinare/{card_id}/{slug}` where slug is derived from the card title via NFKD normalization → ASCII encode → lowercase → `[^a-z0-9_]+` → `-` → strip edges → 50-char cap → fallback `"untitled"`.

**Rationale**:
- `NFKD` + `encode("ascii", errors="ignore")` handles accented Latin characters (é→e, ü→u) without any external dependency.
- 50-char slug cap keeps the total branch name under ~65 chars, safe for all git hosts and UI displays (GitHub truncates at ~48 chars in PR UI).
- The `coordinare/{card_id}/` prefix ensures uniqueness across cards and makes branch-to-card tracing trivial in git history.
- Pure Python stdlib (`unicodedata`, `re`) — no new dependencies.

**Alternatives considered**:
- `python-slugify` library: Handles more unicode edge cases but adds a dependency. Rejected (Minimal Dependencies principle).
- URL-encoding special chars: Valid git branch names but unreadable. Rejected.
- Short UUID suffix: Non-deterministic. Rejected (FR-007 requires determinism).

**Edge cases handled**:
- Empty title: fallback to `"untitled"`
- All-special-char title (e.g., `"---"`): strips to empty → fallback
- CJK/emoji (no ASCII representation): drops to empty → fallback
- Trailing `.lock` after truncation: stripped
- Titles with existing slashes: treated as unsafe, collapsed to `-`

---

## Decision 3: Clone Depth

**Decision**: `--depth=1` (shallow clone) by default.

**Rationale**: For a coding agent that reads the current working tree, creates a branch, writes changes, and pushes, shallow history is sufficient. A shallow clone completes faster and uses significantly less disk space, directly supporting SC-005 (no orphaned workspaces) and the disk space assumption in the spec.

**Alternatives considered**:
- Full clone: Required only for `git log --follow` across file renames or `git blame` beyond depth 1. Claude Code does use `git blame`, which has limited utility on a shallow clone. However, the spec's use case (coding agent working on a new branch from HEAD) rarely requires deep history. A shallow clone can be unshallowed in-place with `git fetch --unshallow` if needed.
- Configurable depth: Adds complexity. Deferred as a future config option.

---

## Decision 4: Workspace Directory Layout

**Decision**: `tempfile.mkdtemp(dir=workspace_root, prefix="coordinare-ws-")` creates a parent container. The actual repo clone lives at `{container}/repo/`. This ensures the parent is always removable even if `git clone` wrote nothing.

**Rationale**: `git clone` creates the destination directory before transferring objects. A failed clone leaves an empty directory. By cloning into a subdirectory of the `mkdtemp` result, the `shutil.rmtree` on the parent container always succeeds regardless of how far the clone progressed.

---

## Decision 5: WorkspaceManager Service Pattern

**Decision**: `WorkspaceManager` is a service class initialized with `ProjectConfiguration`, injected into `CoordinareState` like other services (`agent_service`, `github_service`).

**Rationale**: Consistent with the existing service injection pattern in `__main__.py`. Nodes don't need direct config access — the manager encapsulates all workspace policy. The manager's `prepare()` method is transport-aware: for `subprocess`/`ssh` it clones locally and returns the workspace path; for `kubernetes` it returns no local path (performer self-clones using the dispatch payload).

---

## Decision 6: Cleanup Trigger Point

**Decision**: Workspace teardown is called from `monitor_agent.py` on all terminal session states: `pr_opened`, `blocked`, `error`, `session_expired`.

**Rationale**: `monitor_agent` is the single exit point for all session outcomes. Cleanup here covers all cases in User Story 3. The `workspace_path` is stored in `CoordinareState` during dispatch and read during teardown — the state is the handoff mechanism between the two nodes.

**Cleanup on dispatch failure**: If `dispatch_card.py` calls `workspace_manager.prepare()` and the prepare step fails (clone error), cleanup happens inside `prepare()` before raising `WorkspaceSetupError`. No path is written to state. No separate teardown needed.

---

## Decision 7: No New pip Dependencies

**Decision**: stdlib only — `asyncio`, `unicodedata`, `re`, `tempfile`, `shutil`, `os`, `stat`, `pathlib`. No new packages.

**Rationale**: Minimal Dependencies principle from the constitution. All required operations are well-served by stdlib. `structlog` (existing) covers logging.
