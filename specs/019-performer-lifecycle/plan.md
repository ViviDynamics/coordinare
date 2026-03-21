# Implementation Plan: Performer Lifecycle

**Branch**: `019-performer-lifecycle` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Refactor the coordinare graph to replace the hardcoded single-implementer dispatch/monitor cycle with a generic, configurable multi-role sequential lifecycle. Three new graph nodes replace two old ones: `dispatch_performer` and `monitor_performer` resolve the active service from a `performer_services` registry keyed on `performer_stage`; `classify_human_feedback` routes PR comments back to the earliest affected role. No new Python packages required. Backward compatibility is maintained: an implementer-only config produces identical runtime behavior to today.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: LangGraph ≥ 0.2 (existing), pydantic-settings (existing), structlog (existing), asyncio (stdlib) — **no new dependencies required**
**Storage**: N/A — `CoordinareState` is in-memory; `performers:` key added to existing `config.yaml`
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project — existing `src/coordinare/` layout
**Performance Goals**: Sequential role dispatch adds zero concurrency overhead; graph transitions < 1 ms per hop; no budget concern
**Constraints**: Six fixed board columns (Backlog, TODO, In Progress, Blocked, In Review, Done); all performer stages show "In Progress" to humans; backward-compatible with implementer-only config
**Scale/Scope**: 3 new graph node files, 5 modified files, ~40 new unit tests; no new service dependencies

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `dispatch_performer` and `monitor_performer` contain zero role-specific logic — all role selection is via registry lookup |
| II. Testing Discipline | PASS | All three new nodes require unit tests; lifecycle advancement logic is independently testable |
| III. User Experience | N/A | No UI changes — board column mapping already covers all stages under "In Progress" |
| IV. Performance by Design | PASS | Sequential execution adds no concurrency complexity; each role dispatch is an independent async call |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers remain in spec |

All quality gates are expected to pass. No complexity violations.

## Project Structure

### Documentation (this feature)

```text
specs/019-performer-lifecycle/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

No `contracts/` (no new API surface — this is internal graph refactoring only).

### Source Code (files changed)

```text
src/coordinare/
├── config.py                                    # MODIFIED — PerformerRoleConfig, PerformersConfig, performers field
├── graph/
│   ├── state.py                                 # MODIFIED — performer_stage, performer_services, lifecycle_sequence
│   ├── builder.py                               # MODIFIED — replace dispatch_card/monitor_agent with new nodes; add classify_human_feedback
│   └── nodes/
│       ├── dispatch_performer.py                # NEW — generic dispatch node (replaces dispatch_card)
│       ├── monitor_performer.py                 # NEW — generic monitor node (replaces monitor_agent)
│       ├── classify_human_feedback.py           # NEW — PR comment classification and lifecycle re-entry
│       ├── dispatch_card.py                     # DEPRECATED (kept as thin wrapper for migration period)
│       └── monitor_agent.py                     # DEPRECATED (kept as thin wrapper for migration period)
└── __main__.py                                  # MODIFIED — build performer_services registry and lifecycle_sequence

tests/unit/
├── graph/nodes/
│   ├── test_dispatch_performer.py               # NEW
│   ├── test_monitor_performer.py                # NEW
│   └── test_classify_human_feedback.py          # NEW
└── test_config.py                               # MODIFIED — performer config parsing tests
```

**Structure Decision**: Three new node files rather than modifying the old ones. Old nodes are deprecated (kept as thin delegators) for one release to allow any direct callers to migrate. The graph builder switches to the new nodes immediately.

## Detailed Implementation Plan

### Step 1 — Extend Config Model (`src/coordinare/config.py`)

Add per-role performer configuration:

```python
class PerformerRoleConfig(BaseModel):
    """Configuration for a single performer role's backend."""
    backend: str = "opencode"          # e.g. opencode, claude-code, codex
    transport: str = "subprocess"      # subprocess | ssh | kubernetes
    image: str | None = None           # container image (kubernetes transport)
    # Additional transport-specific fields (host, port, etc.) follow existing pattern

class PerformersConfig(BaseModel):
    """Per-role performer backend configuration."""
    advocate: PerformerRoleConfig | None = None
    assessor: PerformerRoleConfig | None = None
    architect: PerformerRoleConfig | None = None
    implementer: PerformerRoleConfig | None = None
    reviewer: PerformerRoleConfig | None = None
    security: PerformerRoleConfig | None = None
    qa: PerformerRoleConfig | None = None
    tech_writer: PerformerRoleConfig | None = None
```

Add to `ProjectConfiguration`:

```python
performers: PerformersConfig = PerformersConfig()
```

**Lifecycle sequence** is derived at service-bootstrap time (not in Pydantic) by inspecting which roles are non-None in `performers`, in the canonical order:
```
advocate → assessing → architecting → implementing → reviewing → security → qa → documenting
```

**Backward compatibility**: If `performers` is absent from `config.yaml`, `PerformersConfig()` defaults all roles to `None`. The bootstrap treats this as implementer-only (using the existing `agent_service` transport config) to preserve current behavior.

---

### Step 2 — Extend CoordinareState (`src/coordinare/graph/state.py`)

Add three new fields:

```python
# Active performer role in the lifecycle
performer_stage: str  # e.g. "implementing", "reviewing", "qa", "documenting"

# Registry: role name → AgentService instance (built at startup)
performer_services: dict[str, AgentServiceProtocol]

# Ordered list of role names to execute (derived from config at startup)
lifecycle_sequence: list[str]
```

Update `initial_state()` to include these with sensible defaults:
- `performer_stage`: `"implementing"` (backward-compatible default)
- `performer_services`: `{}` (populated by bootstrap)
- `lifecycle_sequence`: `["implementing"]` (backward-compatible default)

---

### Step 3 — New Node: `dispatch_performer` (`src/coordinare/graph/nodes/dispatch_performer.py`)

Replaces `dispatch_card`. Contains zero role-specific logic:

```python
async def dispatch_performer(state: CoordinareState) -> dict:
    stage = state["performer_stage"]
    service = state["performer_services"].get(stage)
    if service is None:
        # No service for this stage — skip and advance
        return _advance_stage(state)

    # Health check
    health = await service.check_health()
    if health.get("status") != "healthy":
        # Same handling as existing dispatch_card unreachable-agent logic
        ...

    # Workspace prep (same as existing dispatch_card)
    ...

    # Dispatch
    result = await service.dispatch_card(card_context, workspace_info)
    ...
    return {"phase": "monitoring_performer", "agent_dispatch": result, ...}
```

The node reads `performer_stage` to resolve the service; it does not branch on role identity.

---

### Step 4 — New Node: `monitor_performer` (`src/coordinare/graph/nodes/monitor_performer.py`)

Replaces `monitor_agent`. Contains zero role-specific logic:

```python
async def monitor_performer(state: CoordinareState) -> dict:
    stage = state["performer_stage"]
    service = state["performer_services"].get(stage)
    session_id = state["agent_dispatch"].get("session_id", "")

    status = await service.check_status(session_id)
    marker = status.get("status")

    # Terminal success states: pr_opened, plan_committed, approved,
    # security_passed, qa_passed, docs_committed
    if marker in TERMINAL_SUCCESS_STATES:
        return _advance_stage(state, status)

    # Terminal failure states: same as today
    if marker == "error":
        ...

    # In-progress states: working, blocked (same as today)
    ...
```

**`_advance_stage(state, status)` logic**:

```python
def _advance_stage(state, status=None):
    sequence = state["lifecycle_sequence"]
    current = state["performer_stage"]
    try:
        idx = sequence.index(current)
    except ValueError:
        idx = len(sequence)  # Treat unknown stage as last

    if idx + 1 < len(sequence):
        # Advance to next role
        return {
            "performer_stage": sequence[idx + 1],
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }
    else:
        # All roles complete — move to human review
        return {
            "phase": "monitoring_pr",
            "current_card": {**state["current_card"], "pr_url": status.get("pr_url", "")},
            ...
        }
```

---

### Step 5 — New Node: `classify_human_feedback` (`src/coordinare/graph/nodes/classify_human_feedback.py`)

Triggered from the `monitoring_pr` loop when new PR comments arrive:

```python
CONCERN_TO_STAGE: dict[str, str] = {
    "implementation": "implementing",
    "architecture": "architecting",
    "security": "security",
    "documentation": "documenting",
    "qa": "qa",
    "review": "reviewing",
}

async def classify_human_feedback(state: CoordinareState) -> dict:
    pr_comments = await github.get_pr_comments(pr_node_id)

    # Use Claude/assessor backend to classify comments
    classification = await claude.classify_pr_feedback(pr_comments)

    if classification.get("approved"):
        # Human approved → Done
        await github.move_card(card_id, "DONE")
        return {"phase": "merging", ...}

    target_concern = classification.get("concern", "implementation")
    target_stage = CONCERN_TO_STAGE.get(target_concern, "implementing")

    if target_stage not in state["lifecycle_sequence"]:
        # Fall back to implementer if target role is not configured
        target_stage = "implementing"

    # Relay PR comments as feedback and re-enter the lifecycle
    return {
        "performer_stage": target_stage,
        "phase": "dispatching",
        "agent_dispatch": {},
        ...
    }
```

---

### Step 6 — Graph Builder (`src/coordinare/graph/builder.py`)

Replace `dispatch_card` and `monitor_agent` with new nodes; add `classify_human_feedback`:

```python
# Replace:
builder.add_node("dispatch_card", dispatch_card)
builder.add_node("monitor_agent", monitor_agent)

# With:
builder.add_node("dispatch_performer", dispatch_performer)
builder.add_node("monitor_performer", monitor_performer)
builder.add_node("classify_human_feedback", classify_human_feedback)
```

Update routing conditions to use `"dispatching"` → `dispatch_performer` and `"monitoring_performer"` → `monitor_performer`. Add `"monitoring_pr"` → `classify_human_feedback` edge when PR comments are detected.

**New phase values**:
- `"monitoring_performer"` (was `"monitoring_agent"`) — polling a performer mid-execution

---

### Step 7 — Service Bootstrap (`src/coordinare/__main__.py`)

Build the `performer_services` registry at startup:

```python
def _build_performer_services(config: ProjectConfiguration, ...) -> dict[str, AgentServiceProtocol]:
    services = {}
    for role, role_config in _iter_configured_roles(config.performers):
        transport = _build_transport(role_config)
        service = AgentService(transport)
        services[role] = ResilientAgentService(service, ...)
    return services

def _build_lifecycle_sequence(config: ProjectConfiguration) -> list[str]:
    canonical = ["advocate", "assessing", "architecting", "implementing",
                 "reviewing", "security", "qa", "documenting"]
    return [r for r in canonical if getattr(config.performers, r.replace("ing",""), None) is not None
            or (r == "implementing" and not _has_any_performers(config.performers))]
```

Set on `initial_state()`:
```python
state["performer_services"] = performer_services
state["lifecycle_sequence"] = lifecycle_sequence
state["performer_stage"] = lifecycle_sequence[0] if lifecycle_sequence else "implementing"
```

---

## Complexity Tracking

No constitution violations.

| Change | Scope | Justification |
|--------|-------|---------------|
| 3 new node files | ~180 LOC total | `dispatch_performer` and `monitor_performer` cannot share a single file cleanly; `classify_human_feedback` is entirely distinct logic |
| Deprecate old nodes | 2 thin wrappers, ~10 LOC each | Preserves any direct test references during migration without breaking the graph |
