# Quickstart: Agent Workspace Management (011)

Integration test scenarios for the workspace feature. These scenarios drive the integration test suite in `tests/integration/test_workspace_integration.py`.

## Prerequisites

- `git` available in `PATH`
- A local bare git repository to use as a clone source (created in test fixtures — no network required)
- The `WorkspaceManager` configured with the bare repo URL and a dummy token

## Scenario 1: Happy Path — Workspace Created and Cleaned Up

**Goal**: Verify that `prepare()` creates a cloned workspace with the correct branch, and `teardown()` removes it.

**Steps**:
1. Create a local bare git repo as the "remote" (test fixture).
2. Instantiate `WorkspaceManager` with `workspace_root=None` (system temp).
3. Call `prepare({"id": "TEST_1", "title": "Add retry logic"})`.
4. Assert returned `WorkspaceInfo.branch == "coordinare/TEST_1/add-retry-logic"`.
5. Assert `WorkspaceInfo.path` exists and is a directory.
6. Assert `git -C path branch --show-current` outputs `coordinare/TEST_1/add-retry-logic`.
7. Assert `.git/config` in workspace contains an authenticated remote URL.
8. Call `teardown(WorkspaceInfo.path)`.
9. Assert the directory no longer exists.

**Expected outcome**: PASS.

---

## Scenario 2: Clone Failure — Card Blocked, No Orphan Directory

**Goal**: Verify that a clone failure raises `WorkspaceSetupError` and leaves no partial directory.

**Steps**:
1. Configure `WorkspaceManager` with an invalid repo URL (e.g., `https://github.com/nonexistent/repo.git`).
2. Call `prepare({"id": "TEST_2", "title": "Fix bug"})`.
3. Assert `WorkspaceSetupError` is raised.
4. Assert no directory with the `coordinare-ws-` prefix exists under `workspace_root`.

**Expected outcome**: `WorkspaceSetupError` raised, temp dir cleaned up.

---

## Scenario 3: Concurrent Workspaces Are Isolated

**Goal**: Verify two concurrent `prepare()` calls produce distinct directories.

**Steps**:
1. Use `asyncio.gather` to call `prepare()` for two cards simultaneously.
2. Assert the two returned `workspace_path` values are different absolute paths.
3. Assert both directories exist and contain independent git repos.
4. Teardown both.

**Expected outcome**: Two distinct, isolated workspace directories.

---

## Scenario 4: Deterministic Branch Naming

**Goal**: Verify `make_branch_name` is deterministic across repeated calls.

**Steps** (unit test, no I/O):
1. Call `make_branch_name("PVTI_abc", "Add retry logic")` three times.
2. Assert all three return identical strings.
3. Assert the returned string matches `coordinare/PVTI_abc/add-retry-logic`.

**Expected outcome**: Identical result every time.

---

## Scenario 5: Teardown Survives Missing Directory

**Goal**: Verify that `teardown()` logs a warning and does not raise when the directory is already gone.

**Steps**:
1. Call `teardown(Path("/tmp/coordinare-ws-does-not-exist"))`.
2. Assert no exception is raised.
3. Assert a warning is logged with `workspace_cleanup_failed`.

**Expected outcome**: Silent warning, no exception.

---

## Scenario 6: Workspace Root PVC Simulation

**Goal**: Verify that `workspace_root` directs workspace creation to a specified parent directory.

**Steps**:
1. Create a temp directory to simulate a PVC mount: `pvc_mount = Path(tempfile.mkdtemp())`.
2. Configure `WorkspaceManager` with `workspace_root=pvc_mount`.
3. Call `prepare({"id": "TEST_6", "title": "Feature"})`.
4. Assert `workspace_path` is a subdirectory of `pvc_mount`.
5. Call `teardown(workspace_path)`.
6. Assert `pvc_mount` still exists (teardown removes only the workspace subdir).

**Expected outcome**: Workspace created under the configured root, PVC parent unaffected by teardown.
