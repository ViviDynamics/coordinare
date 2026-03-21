# Data Model: Performer Lifecycle

**Branch**: `019-performer-lifecycle` | **Date**: 2026-03-18

## CoordinareState Extensions

Three new fields are added to the existing `CoordinareState` TypedDict:

### `performer_stage: str`

The name of the currently active performer role in the lifecycle.

| Value | Role | Added by |
|-------|------|----------|
| `"implementing"` | Implementer | existing (012) |
| `"architecting"` | Architect | 020 |
| `"reviewing"` | Reviewer | 021 |
| `"security"` | Security | 022 |
| `"qa"` | QA | 023 |
| `"documenting"` | Tech Writer | 024 |
| `"assessing"` | Assessor (future) | TBD |
| `"advocate"` | Advocate (future) | TBD |

**Default**: `"implementing"` (backward-compatible)

**Config field → Stage mapping**:

| Config field (`PerformersConfig`) | `performer_stage` value | Display name |
|-----------------------------------|------------------------|--------------|
| `advocate` | `"advocate"` | Advocate |
| `assessor` | `"assessing"` | Assessor |
| `architect` | `"architecting"` | Architect |
| `implementer` | `"implementing"` | Implementer |
| `reviewer` | `"reviewing"` | Reviewer |
| `security` | `"security"` | Security |
| `qa` | `"qa"` | QA |
| `tech_writer` | `"documenting"` | Tech Writer |

The mapping is defined in `_build_lifecycle_sequence()` in `__main__.py`.

---

### `performer_services: dict[str, AgentServiceProtocol]`

Registry mapping role name → configured `AgentService` instance. Built once at daemon startup from `config.yaml`. Nodes resolve the active service via `performer_services[performer_stage]`.

**Default**: `{}` — populated by `_build_performer_services()` at startup. If empty, bootstrap falls back to the legacy single `agent_service` field for backward compatibility.

---

### `lifecycle_sequence: list[str]`

Ordered list of `performer_stage` values to execute for the current card. Derived at startup from config; stored in state so nodes do not need to recompute it.

**Example (full lifecycle)**: `["architecting", "implementing", "reviewing", "security", "qa", "documenting"]`

**Example (implementer-only, backward compat)**: `["implementing"]`

**Default**: `["implementing"]`

---

## Config Entities

### PerformerRoleConfig

Pydantic `BaseModel`. Configuration for one performer role's backend. All fields are optional transport-specific overrides; defaults match the existing `agent_transport` config.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | `str` | `"opencode"` | AI coding backend identifier |
| `transport` | `str` | `"subprocess"` | Transport type: subprocess, ssh, kubernetes |
| `image` | `str \| None` | `None` | Container image (kubernetes transport only) |
| `executable` | `str \| None` | `None` | Overrides `agent_executable` for this role |
| `host` | `str \| None` | `None` | SSH/K8s host override |
| `port` | `int \| None` | `None` | Port override |
| `timeout_seconds` | `int \| None` | `None` | Overrides `transport_timeout_seconds` |

---

### PerformersConfig

Pydantic `BaseModel`. Container for all role configs. A role is considered configured when its field is non-None.

| Field | Type | Default |
|-------|------|---------|
| `advocate` | `PerformerRoleConfig \| None` | `None` |
| `assessor` | `PerformerRoleConfig \| None` | `None` |
| `architect` | `PerformerRoleConfig \| None` | `None` |
| `implementer` | `PerformerRoleConfig \| None` | `None` |
| `reviewer` | `PerformerRoleConfig \| None` | `None` |
| `security` | `PerformerRoleConfig \| None` | `None` |
| `qa` | `PerformerRoleConfig \| None` | `None` |
| `tech_writer` | `PerformerRoleConfig \| None` | `None` |

Added to `ProjectConfiguration`:

```python
performers: PerformersConfig = PerformersConfig()
```

---

## Lifecycle State Machine

```
[idle]
  │ card ready
  ▼
[dispatching]  ◄─────────────────────────────────────────────────────┐
  │                                                                   │
  │ dispatch_performer                                                 │
  ▼                                                                   │
[monitoring_performer]                                                 │
  │                                                                   │
  ├── working / blocked ──────────────────────────────────────────────┘ (loop)
  │
  ├── terminal_success AND more roles remain ──▶ advance performer_stage ──▶ [dispatching]
  │
  └── terminal_success AND no more roles ──────▶ [monitoring_pr]
        │
        ├── new PR comments ──▶ classify_human_feedback
        │       │
        │       ├── implementation/arch/security/docs concern ──▶ reset performer_stage ──▶ [dispatching]
        │       │
        │       └── human approved ──▶ [merging] ──▶ [done]
        │
        └── PR merged ──▶ [done]
```

---

## Terminal Success States

| State string | Emitted by role | Triggers |
|-------------|----------------|----------|
| `pr_opened` | implementer | lifecycle advancement |
| `plan_committed` | architect | lifecycle advancement |
| `approved` | reviewer | lifecycle advancement |
| `security_passed` | security | lifecycle advancement |
| `qa_passed` | QA | lifecycle advancement |
| `docs_committed` | tech writer | transition to monitoring_pr |

---

## Phase Values (complete set after 019)

| Phase | Meaning |
|-------|---------|
| `idle` | No active card; waiting |
| `dispatching` | About to dispatch to `performer_stage` |
| `monitoring_performer` | Polling active performer session |
| `monitoring_pr` | All automated roles done; waiting for human review |
| `relay_feedback` | Routing feedback back to a performer |
| `blocked` | Performer blocked; waiting for human input |
| `merging` | Human approved; merging PR |
| `recovery` | Transient error recovery |
| `system_error` | Unrecoverable error |
