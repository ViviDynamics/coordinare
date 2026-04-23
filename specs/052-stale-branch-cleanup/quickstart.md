# Quickstart: Stale Branch Cleanup

## Scenario 1 — Stale branch deleted before dispatch

### Setup
1. Manually create branch `feat/pvti-abc-fix-login` on the remote:
   ```bash
   git push origin main:refs/heads/feat/pvti-abc-fix-login
   ```
2. Dispatch the corresponding card (with matching card ID and title)

### Expected
- Coordinare detects stale branch, deletes it
- Log entry: `workspace.stale_branch_delete_attempted` with branch and card_id
- Performer creates a fresh branch from main
- PR created successfully (no 422 error)

---

## Scenario 2 — No stale branch (normal path)

### Setup
1. First dispatch for a card (no pre-existing branch)

### Expected
- `branch_exists` returns 404 quickly (<50ms); workspace creation proceeds normally.

---

## Scenario 3 — Suffix strategy (opt-in)

### Setup
1. Set `branch_collision_strategy: suffix` in config
2. Branch `feat/pvti-abc-fix-login` already exists on remote
3. `feat/pvti-abc-fix-login-2` does NOT exist

### Expected
- Coordinare checks main branch → exists
- Checks `-2` variant → doesn't exist
- Creates workspace with branch `feat/pvti-abc-fix-login-2`
- Log: `workspace.branch_suffix_applied`

---

## Scenario 4 — Cleanup disabled

### Setup
1. Set `stale_branch_cleanup: false` in config
2. Stale branch exists

### Expected
- No branch_exists check, no delete
- Performer may fail to push if branch exists with conflicting commits (same behavior as pre-052 coordinare)
