# Implementation Plan: Operational Visibility & Hygiene

**Branch**: `052-stale-branch-cleanup` | **Date**: 2026-04-21 | **Spec**: specs/052-stale-branch-cleanup/spec.md

## Summary

Three independent operational improvements implemented together as a single branch:

1. **Backend Transparency** — surface the performer backend's live UI URL and session stats in the dashboard SSE payload and performer panel.
2. **Stale Branch Cleanup** — detect and delete (or suffix) stale remote branches before workspace creation to prevent 422 PR failures.
3. **Contract Enforcement in Speckit** — extend `speckit.analyze` to cross-reference spec/plan content against `specs/contracts/*.md` and flag unregistered cross-boundary payload fields.

## Technical Context

**Language/Version**: Python 3.12+ (features 1 & 2); YAML/Markdown analysis + skill update (feature 3)
**Primary Dependencies**: httpx (existing), pydantic-settings (existing), structlog (existing), FastAPI/SSE (existing), vanilla JS dashboard (existing)
**Storage**: N/A for all three
**Testing**: pytest (existing) for features 1 & 2; speckit integration for feature 3
**Performance Goals**:
- Feature 1: backend stats poll ≤ 30 s interval; non-blocking; errors swallowed
- Feature 2: +1 GitHub REST call per dispatch (branch_exists); DELETE only when stale
- Feature 3: speckit.analyze runtime impact < 1 s

## Constitution Check

| Gate | Status | Notes |
|------|--------|-------|
| Lint & Format | ✓ PASS | ruff enforced |
| Type Check | ✓ PASS | All new fields typed |
| Unit Tests | ✓ PASS | Mock GitHub REST; mock SSE payloads |
| Coverage | ✓ PASS | New paths covered |
| No dead code | ✓ PASS | |

## Project Structure

```text
src/coordinare/
├── config.py                          # Add stale_branch_cleanup, branch_collision_strategy
├── session.py                         # Add SessionStats dataclass + backend_ui_url/session_stats to CardSession
├── services/github.py                 # Add branch_exists() + delete_branch() REST methods
├── transport/subprocess_transport.py  # Add _discover_backend_ui_url() + _fetch_session_stats() helpers
├── graph/nodes/monitor_performer.py   # Wire backend URL discovery + stats polling into monitor loop
├── workspace.py                       # Stale branch check in prepare() before clone
└── dashboard.py                       # Include backend_ui_url + session_stats in SSE state

specs/contracts/
└── dispatch-payload.md                # Existing contract registry — speckit.analyze reads this

.claude/commands/speckit.analyze.md    # Extended with contract cross-reference check
.claude/commands/speckit.contracts.md  # New maintenance skill (contracts vs code diff)

tests/unit/
├── test_workspace.py                  # Stale branch detection + both strategies
├── test_github_service.py             # branch_exists + delete_branch
├── test_subprocess_transport.py       # _discover_backend_ui_url + _fetch_session_stats
└── test_monitor_performer.py          # Backend URL wiring + 30 s throttle logic
```

## Implementation Notes

### Feature 1: Backend Transparency

#### 1a. Backend UI URL discovery

Each backend type has a known mechanism for exposing its local UI:
- **opencode**: listens on a random port; the port is written to a temp file or emitted on stderr as a structured log line. Parse the port from performer stderr logs (already buffered in `SubprocessTransport._agent_logs`) by scanning for a pattern like `"server":"http://127.0.0.1:<port>"`.
- **claude_code**: no local UI — `backend_ui_url` is `null`.
- **custom backends**: no discovery; `backend_ui_url` is `null` unless the config provides a `backend_ui_port`.

Add `backend_ui_url: str | None` to the per-session state tracked by the orchestrator or a new `PerformerSession` helper.

#### 1b. Session stats polling

For opencode, once the port is known, poll `http://127.0.0.1:{port}/api/session` (or equivalent) at most every 30 seconds. Map the response to `SessionStats`:

```python
@dataclass
class SessionStats:
    title: str | None
    files_changed: int
    lines_added: int
    lines_removed: int
```

Cache the last successful result. Any HTTP error → keep last cache, log at DEBUG.

#### 1c. SSE state extension

In `dashboard.py` (or the SSE state builder), add `backend_ui_url` and `session_stats` per performer entry in the state dict sent to clients.

#### 1d. Dashboard JS panel update

In the dashboard HTML/JS, render `backend_ui_url` as `<a href="{url}" target="_blank">Open in browser</a>` when non-null. Render `session_stats` as a compact line: `"{title} · {files_changed} files · +{lines_added}/-{lines_removed}"`.

---

### Feature 2: Stale Branch Cleanup

#### 2a. Config in `config.py`

```python
from enum import Enum

class BranchCollisionStrategy(str, Enum):
    delete = "delete"
    suffix = "suffix"

# In CoordinareConfig / ProjectConfiguration:
stale_branch_cleanup: bool = True
branch_collision_strategy: BranchCollisionStrategy = BranchCollisionStrategy.delete
```

#### 2b. `GitHubService` REST methods

```python
async def branch_exists(self, branch_name: str) -> bool:
    # GET /repos/{owner}/{repo}/branches/{branch_name}
    # Returns True if 200, False if 404; raises on other errors

async def delete_branch(self, branch_name: str) -> None:
    # DELETE /repos/{owner}/{repo}/git/refs/heads/{branch_name}
    # Logs warning on failure, doesn't raise (non-fatal)
```

Uses existing `self._http_client` (httpx) and token. Respects `self._github_api_url` for GHE (spec 036).

#### 2c. `workspace.py.prepare()` — stale branch detection

After computing `branch`, before the clone block:

```python
if getattr(self._config, "stale_branch_cleanup", True):
    strategy = getattr(self._config, "branch_collision_strategy", "delete")
    if await self._github_service.branch_exists(branch):
        if strategy == "suffix":
            for i in range(2, 10):
                candidate = f"{branch}-{i}"
                if not await self._github_service.branch_exists(candidate):
                    branch = candidate
                    break
            else:
                await self._github_service.delete_branch(branch)
                logger.warning("workspace.suffix_exhausted_delete_attempted", branch=branch)
        else:
            await self._github_service.delete_branch(branch)
            logger.info("workspace.stale_branch_delete_attempted", branch=branch, card_id=card_id)
```

`WorkspaceManager` needs a stored reference to `GitHubService`. Pass it in from `__main__.py` alongside the existing `auth` parameter.

---

### Feature 3: Contract Enforcement in Speckit

#### 3a. Contract registry format

`specs/contracts/*.md` files already exist (e.g. `dispatch-payload.md`). Each file contains a `## Field Registry` section with a markdown table of fields. The analyze skill will:

1. Read all `specs/contracts/*.md` files.
2. Parse each `## Field Registry` table to extract field names.
3. Scan the feature's `spec.md` and `plan.md` for mentions of those payload field names in the context of additions/changes.
4. If a field name appears in spec/plan but is not in the contract registry table, emit a `[CONTRACT MISMATCH]` warning.

#### 3b. `speckit.analyze` extension

Add a new check section to the analyze skill prompt/template:

```
### Contract Check
For each file in specs/contracts/:
  - Parse the Field Registry table to get known field names
  - Scan spec.md and plan.md for NEW field names mentioned in the context of payload changes
  - If a new field name does not appear in the contract file, emit:
    [CONTRACT MISMATCH] Field `{field_name}` referenced in spec/plan but not registered in {contract_file}
```

#### 3c. `speckit.contracts` command (new skill or analyze flag)

A lightweight new skill that:
1. Reads `specs/contracts/*.md` to get the expected field registry.
2. Greps the source code for the corresponding Pydantic models (e.g. `Score`, `ProtocolMessage`).
3. Compares field sets and reports: fields in code but not in contract, fields in contract but not in code.

This is a developer-facing maintenance tool, not part of the normal speckit pipeline.

## Complexity Tracking

No violations. Feature 1 is the largest (SSE extension + JS update + backend polling) but all changes are additive and isolated.
