# Phase 1 Design: Data Model & Entity Relationships

**Date**: 2026-04-30  
**Spec Reference**: [spec.md](spec.md)

## Core Entity Hierarchy

```
CoordinareConfiguration (YAML config.yaml)
├── global_config: ProjectConfiguration
│   ├── github_token
│   ├── github_org
│   ├── default_performer_persona (per spec 018)
│   ├── notification_config
│   └── [all existing ProjectConfiguration fields]
│
├── symphonies: List[SymphonyConfig]  [ordered list, order = priority]
│   ├── name: str (unique, e.g., "myapp-backend")
│   ├── github_project_number: int (board reference)
│   ├── overrides: ProjectConfiguration (optional, merged with global_config)
│   └── personas: Dict[str, PersonaOverride] (optional, per spec 018)
│
└── orchestra: OrchestraConfig
    ├── mode: Literal["shared_pool"]
    └── performers: List[PerformerSpec] (provisioned at startup, shared across all symphonies)
```

## Entity Definitions

### SymphonyConfig (pydantic model)

```python
class SymphonyConfig(BaseModel):
    """A named project orchestration configuration.
    
    A symphony is a single GitHub project board with its own coordinare loop,
    drawing performers from a shared orchestra pool.
    """
    
    name: str  # "myapp-backend", alphanumeric + dash
    github_project_number: int  # 42 (the board ID)
    
    # Effective config resolved via: merge(global_config, overrides)
    overrides: ProjectConfiguration | None = None
    
    # Per-symphony personas (NEW — extends spec 018)
    personas: Dict[str, PersonaOverride] | None = None
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    
    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", v):
            raise ValueError("name must be alphanumeric + dash, start/end with alphanumeric")
        return v
    
    def effective_config(self, global_config: ProjectConfiguration) -> ProjectConfiguration:
        """Resolve effective config by merging global with overrides."""
        base_dict = global_config.model_dump()
        if self.overrides:
            base_dict.update(self.overrides.model_dump(exclude_unset=True))
        return ProjectConfiguration(**base_dict)
```

### OrchestraConfig (pydantic model)

```python
class OrchestraConfig(BaseModel):
    """Shared performer pool configuration.
    
    The orchestra is a pool of performers (agents/executors) shared by all symphonies.
    Performers are allocated to symphonies on demand per cycle based on availability.
    """
    
    mode: Literal["shared_pool"] = "shared_pool"  # Future: "per_symphony_pool", "hybrid"
    performers: List[PerformerSpec] = Field(default_factory=list)
    
    # Allocation strategy (future)
    allocation_strategy: Literal["round_robin", "priority_order"] = "priority_order"
```

### CoordinareConfiguration (extended, pydantic model)

```python
class CoordinareConfiguration(BaseModel):
    """Root configuration combining global defaults + multiple symphonies + orchestra."""
    
    # Global defaults applied to all symphonies (when merged via merge())
    global_config: ProjectConfiguration
    
    # List of orchestrated projects (order = priority for performer allocation)
    symphonies: List[SymphonyConfig]
    
    # Shared performer pool
    orchestra: OrchestraConfig = Field(default_factory=OrchestraConfig)
    
    @field_validator("symphonies")
    def validate_unique_names(cls, v: List[SymphonyConfig]) -> List[SymphonyConfig]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("symphony names must be unique")
        return v
    
    @field_validator("symphonies")
    def validate_unique_boards(cls, v: List[SymphonyConfig]) -> List[SymphonyConfig]:
        boards = [s.github_project_number for s in v]
        if len(boards) != len(set(boards)):
            raise ValueError("symphony project numbers must be unique")
        return v
```

## CoordinareState Extensions

**File**: `src/coordinare/daemon.py` → `CoordinareState` dataclass

### New Fields

```python
@dataclass
class CoordinareState:
    # ... existing fields ...
    
    # 057: Multi-symphony orchestration
    symphony_configs: dict[str, SymphonyConfig]  # keyed by symphony.name
    symphony_states: dict[str, SymphonyRuntimeState]  # per-symphony runtime tracking
    current_symphony: str | None  # name of symphony currently being orchestrated
    global_config: ProjectConfiguration  # the base/default config
    orchestra_config: OrchestraConfig  # shared performer pool config
    config_version: int  # incremented on hot-reload for change detection
    
    # Subsystems (existing pattern)
    # ... other subsystems ...
```

### SymphonyRuntimeState (new dataclass)

```python
@dataclass
class SymphonyRuntimeState:
    """Per-symphony runtime tracking."""
    
    name: str
    last_poll_at: datetime | None = None
    active_card: Card | None = None
    active_sessions: dict[str, SessionState] = field(default_factory=dict)
    cycle_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    
    # Observability
    board_snapshot: dict[str, list] | None = None  # column → card list
    session_skip_reasons: dict[str, str] | None = None  # per spec 054
```

## Backward Compatibility

### Existing Single-Project Mode

When config.yaml contains **no** `symphonies:` key (legacy config):
- Treat as implicit single symphony named `"default"` (alphanumeric, passes the name validator)
- Map legacy `ProjectConfiguration` root fields to:
  ```
  global_config: ProjectConfiguration(...)
  symphonies:
    - name: "default"
      github_project_number: <from global>
      overrides: null
  ```
- Coordinare operates on this implicit symphony identically to pre-057
- Note: `"__default__"` seen in metrics/logs is the *observability context label* bound when no symphony is actively running (idle periods), not the config-level symphony name

### Config Discovery

**File**: `src/coordinare/config_discovery.py` → `validate_config()`

Enhanced to:
1. Check if `symphonies:` key exists in YAML
2. If absent: auto-wrap as single `"default"` symphony
3. Proceed with multi-symphony flow
4. Result: zero breaking changes for existing deployments

## Configuration File Layout

### Legacy (Single Project)

```yaml
github_token: ${GITHUB_TOKEN}
github_org: myorg
github_project_number: 42
assignee_filter: coordinare-bot
# ... other fields ...
```

### New (Multiple Projects)

```yaml
github_token: ${GITHUB_TOKEN}
github_org: myorg
assignee_filter: coordinare-bot
notification_config:
  slack_webhook: ${SLACK_WEBHOOK}
# ... other global fields ...

symphonies:
  - name: backend
    github_project_number: 42
    overrides:
      assignee_filter: coordinare-backend  # override global

  - name: frontend
    github_project_number: 43
    personas:
      "main-persona": PersonaOverride(...)

orchestra:
  mode: shared_pool
  performers:
    - id: p1
      mode: subprocess
      # ...
```

## Resolution & Effective Configuration

### Effective Symphony Config

```python
def get_effective_config(symphony: SymphonyConfig, global_config: ProjectConfiguration) -> ProjectConfiguration:
    """Resolve what config a symphony actually uses.
    
    1. Start with global_config
    2. Override with symphony.overrides (where set)
    3. Result: effective ProjectConfiguration for this symphony
    """
    base = global_config.model_dump()
    if symphony.overrides:
        overrides = symphony.overrides.model_dump(exclude_unset=True)
        base.update(overrides)
    return ProjectConfiguration(**base)
```

### Persona Resolution (per spec 018)

For a symphony:
1. Check `symphony.personas[role]` → if set, use
2. Else check `global_config.personas[role]` → if set, use
3. Else use default persona for role

## Migration Path (Future)

When moving from single to multi-project:
1. Save current `config.yaml` as backup
2. Create new `config.yaml` with:
   - Existing fields moved to `global_config:`
   - Add `symphonies:` list with one entry (named after the org/project or user choice)
   - Add `orchestra:` with existing performer specs
3. Validate with `/api/config/validate` (new endpoint)
4. Trigger hot-reload via `/api/config/reload` (new endpoint)
5. Confirm coordinare still orchestrates same board

