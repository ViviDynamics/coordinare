# Implementation Plan: Agent Workspace Management

**Branch**: `011-agent-workspace` | **Date**: 2026-03-03 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/011-agent-workspace/spec.md`

## Summary

Before dispatching a card to an AI coding agent, coordinare clones the target GitHub repository into an isolated temporary directory, creates a deterministic branch, and configures git credentials. The workspace path, branch name, and repo URL are injected into the dispatch payload. After the session terminates (any outcome), the workspace directory is removed. For Kubernetes transport, the coordinare provides `repo_url` and `branch` in the payload but does not clone locally — the performer container handles its own workspace using credentials supplied via Kubernetes Secrets. No new pip dependencies are introduced.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `asyncio` (stdlib), `unicodedata` (stdlib), `re` (stdlib), `tempfile` (stdlib), `shutil` (stdlib), `os` + `stat` (stdlib), `pathlib` (stdlib), `structlog` (existing), `pydantic` + `pydantic-settings` (existing)
**Storage**: Ephemeral temp directories under `workspace_root` (default: system temp dir)
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux (K8s/bare metal), macOS (dev)
**Performance Goals**: Workspace setup completes within 120 seconds on a standard network connection for repositories up to 1 GB with `--depth=1`. Teardown completes within 5 seconds under normal conditions.
**Constraints**: No new pip dependencies. No new top-level workflow phases. Cleanup failure must never block the main workflow.
**Scale/Scope**: One workspace per concurrently dispatched card; typical load is 1–5 concurrent cards.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality | ✓ PASS | `workspace.py` has single responsibility; `make_branch_name` is a pure function |
| II. Testing Discipline | ✓ PASS | Unit tests for branch naming, mock git ops, teardown; integration tests against local bare repo |
| III. UX Consistency | N/A | No user-facing UI changes |
| IV. Performance by Design | ✓ PASS | Clone timeout 120s budget defined; teardown 5s budget from spec SC-003 |
| V. Clarity Before Action | ✓ PASS | All decisions resolved in research.md; no NEEDS CLARIFICATION markers remain |

**Post-design re-check**: ✓ PASS — no violations identified.

## Project Structure

### Documentation (this feature)

```text
specs/011-agent-workspace/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code

```text
src/coordinare/
├── workspace.py                        # NEW — WorkspaceManager + branch naming
├── config.py                           # MODIFIED — workspace_root, performer_image fields
├── graph/
│   ├── state.py                        # MODIFIED — workspace_manager, workspace_path fields
│   └── nodes/
│       ├── dispatch_card.py            # MODIFIED — workspace setup before dispatch
│       └── monitor_agent.py            # MODIFIED — workspace teardown on session end
├── services/
│   └── agent_service.py               # MODIFIED — workspace fields in dispatch payload
└── __main__.py                        # MODIFIED — WorkspaceManager instantiation + injection

tests/
├── unit/
│   ├── test_workspace.py              # NEW — branch naming, mock git, teardown
│   └── graph/nodes/
│       ├── test_dispatch_card.py      # MODIFIED — workspace_manager mock
│       └── test_monitor_agent.py      # MODIFIED — teardown verification
└── integration/
    └── test_workspace_integration.py  # NEW — real git ops against local bare repo
```

**Structure Decision**: Single project (existing layout). New `workspace.py` module at top-level of `src/coordinare/` alongside `config.py`, `protocol.py`, and other service modules.

## Phase 0: Research

Complete. See [research.md](./research.md).

Key decisions:
- Credentials via `x-access-token:{token}` in remote URL after clone (token confined to ephemeral workspace-local `.git/config`)
- Branch format: `coordinare/{card_id}/{slug}` with NFKD normalization, 50-char slug cap
- `--depth=1` shallow clone by default
- `tempfile.mkdtemp` container → clone into `{container}/repo/` subdir
- `WorkspaceManager` service injected into `CoordinareState`
- Teardown triggered from `monitor_agent.py` on all terminal states

## Phase 1: Design

### Data Model

See [data-model.md](./data-model.md).

Key entities:
- **WorkspaceInfo**: transient value object (`path: Path | None`, `branch: str`, `repo_url: str`) — returned by `prepare()`, stored in state
- **WorkspaceManager**: stateful service (`github_org`, `project_name`, `github_token`, `workspace_root`, `agent_transport`)
- **WorkspaceSetupError**: typed exception for BLOCK routing

### Config Changes (`config.py`)

Two new optional fields on `ProjectConfiguration`:

```python
workspace_root: Path | None = Field(default=None)
performer_image: str = Field(default="")
```

- `workspace_root`: when set, all `mkdtemp` calls use this as parent. Supports PVC mount paths in K8s. When `None`, falls back to `tempfile.gettempdir()`.
- `performer_image`: used by K8s transport to determine which container image to schedule. Empty string = K8s transport not configured. Not validated unless `agent_transport == "kubernetes"`.

### State Changes (`graph/state.py`)

Three new fields in `CoordinareState`:

```python
workspace_manager: WorkspaceManagerProtocol | None
workspace_path: Path | None
workspace_branch: str | None
```

`WorkspaceManagerProtocol` is a `Protocol` with `prepare` and `teardown` methods, enabling test mocking without importing `WorkspaceManager`.

### `workspace.py` API

```python
# Pure function — no I/O, fully testable
def make_branch_name(card_id: str, card_title: str) -> str:
    """Returns 'coordinare/{card_id}/{slug}'. Deterministic, git-safe."""

class WorkspaceSetupError(RuntimeError):
    """Raised when workspace preparation fails. Message is human-readable for BLOCKED card."""

@dataclass
class WorkspaceInfo:
    path: Path | None    # None for kubernetes transport
    branch: str
    repo_url: str

class WorkspaceManager:
    def __init__(self, config: ProjectConfiguration) -> None: ...

    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        """Clone repo, create branch, configure credentials.
        For kubernetes transport: returns WorkspaceInfo(path=None, ...) without cloning.
        Raises WorkspaceSetupError on failure; no partial dirs left behind."""

    async def teardown(self, path: Path) -> None:
        """Remove workspace directory. Logs warning on failure, never raises."""
```

Internal helpers (private, not exported):
- `async _run_git(*args, cwd, env, timeout)` — mirrors `SubprocessTransport` pattern
- `_make_git_env(token)` — builds env dict with `GIT_TERMINAL_PROMPT=0`

### `dispatch_card.py` Changes

Before calling `agent.dispatch_card(card)`:
1. Get `workspace_manager` from state (if `None`, skip workspace setup — future-proofing for mock/test environments without a manager).
2. Call `await workspace_manager.prepare(card)` → `WorkspaceInfo`.
3. On `WorkspaceSetupError`: move card to BLOCKED (existing pattern), add error message to `open_questions`, return. No `workspace_path` stored in state.
4. On success: store `workspace_info.path` in `state["workspace_path"]`, `workspace_info.branch` in `state["workspace_branch"]`.
5. Pass `workspace_info` to `agent.dispatch_card(card, workspace_info=workspace_info)`.

### `monitor_agent.py` Changes

When session enters a terminal state (`pr_opened`, `blocked`, `error`, `session_expired`), before returning:
1. Get `workspace_manager` and `workspace_path` from state.
2. If both are set, call `await workspace_manager.teardown(workspace_path)`.
3. Clear `state["workspace_path"]` to `None` regardless of teardown outcome.

### `agent_service.py` Changes

`dispatch_card` gains a `workspace_info: WorkspaceInfo | None = None` parameter:

```python
async def dispatch_card(
    self,
    card_context: dict[str, Any],
    workspace_info: WorkspaceInfo | None = None,
) -> dict[str, Any]:
    payload = {
        "title": ...,
        "description": ...,
        "acceptance_criteria": ...,
        "board_card_id": ...,
        "column": ...,
    }
    if workspace_info is not None:
        payload["repo_url"] = workspace_info.repo_url
        payload["branch"] = workspace_info.branch
        if workspace_info.path is not None:
            payload["workspace_path"] = str(workspace_info.path)
    ...
```

`github_token` is NOT included in the payload for subprocess/SSH (credentials are baked into the workspace). For K8s, the performer needs the token to self-clone; the K8s transport implementation (spec 012) will handle injecting the token as a K8s Secret, not via the JSON payload.

### Protocol Contract

The `dispatch` action payload gains three optional fields (backward-compatible since `payload` is `dict[str, Any]`):

| Field | Type | Present when |
|-------|------|--------------|
| `repo_url` | `str` | Always (all transports) |
| `branch` | `str` | Always (all transports) |
| `workspace_path` | `str` | subprocess and SSH transports only |

See [contracts/dispatch-payload.md](./contracts/dispatch-payload.md).

### Quickstart

See [quickstart.md](./quickstart.md) for integration test scenarios.

## Complexity Tracking

No constitution violations. No complexity justification required.
