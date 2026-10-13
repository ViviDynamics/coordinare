# Implementation Plan: No Daemon Clone for Self-Cloning Performers

**Branch**: `107-no-daemon-clone` | **Date**: 2026-10-13 | **Spec**: [spec.md](./spec.md)

## Summary

`WorkspaceManager.prepare` gains `local_clone: bool = True`. With `False` it takes the path the Kubernetes transport already takes: resolve branch and token, return `WorkspaceInfo(path=None, ...)`, run no git. `dispatch_performer._setup_workspace` passes `False` when `_daemon_clone_needed` says nothing on the daemon side needs the tree. `HTTPPerformerService` declares `clones_own_workspace = True`. The persona classifier reads project context through `get_file_content` when there is no workspace path.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12)
**Primary Dependencies**: existing only (`WorkspaceManager`, `HTTPPerformerService`, `ResilientAgentService`, `persona_classifier`, `GitHubService.get_file_content`)
**Storage**: none; no schema change
**Testing**: pytest, ruff, mypy
**Constraints**: no behavior change for the subprocess transport, the Kubernetes transport, or a prose-path security stage

## Constitution Check

- Code Quality: one flag and one decision function; the no-clone path is the one Kubernetes already exercises in production. Pass.
- Testing: every branch of the decision, the wrapper lookup, the manager, and the classifier fallback are unit tested. Pass.
- UX consistency: two new log events (`workspace.daemon_clone_skipped`, `security_floor.daemon_side_scan`); none renamed. Pass.
- Performance: removes a clone per dispatch; adds at most two small API reads per classification when there is no clone. Pass.
- Clarity: the residual daemon-side scanner is stated in the spec and warned about at runtime. Pass.

## Research

- **Every consumer of `workspace_path` already tolerates `None`**: teardown sites, `agent_service` (payload key omitted), the classifier (empty context), the scanner floor (fails closed). That is how the Kubernetes transport runs today.
- **Why not skip the clone for the prose-path security stage too**: `_run_security_floor` returns a synthetic critical `scanner_unavailable` finding when there is no workspace (083 FR-008). Skipping would fail every such dispatch closed.
- **Why a class attribute and not a transport check**: the dispatch node holds the selected service, possibly wrapped, and does not know the transport. An explicit marker is also safe against mocks.
- **`inspect.signature` at the protocol boundary**: per AGENTS.md, optional parameters are passed only to implementations that accept them.

## Project Structure

```text
src/coordinare/
├── workspace.py                          # prepare(local_clone=...)
├── graph/state.py                        # protocol signature
├── graph/nodes/dispatch_performer.py     # _performer_clones_own_workspace, _daemon_clone_needed, _setup_workspace
├── services/http_performer_service.py    # clones_own_workspace = True
└── services/persona_classifier.py        # _fetch_project_context
tests/unit/test_107_no_daemon_clone.py
```
