# Contract: `persona_scope` Config Block Schema

Pydantic models live in `src/coordinare/config.py` (extending the existing `PersonaConfig` and adding `PersonaScopeConfig` as a sibling of `personas`).

## YAML shape (operator-facing)

```yaml
symphony:
  persona_scope:                                   # 074: symphony-level (opt-in)
    enabled: true                                  # required to be true for the feature to fire; default false
    path_classes:                                  # PROJECT-OWNED — coordinare ships no defaults
      docs:               ["*.md", "docs/**/*"]
      config:             ["config/**/*.yaml", "*.toml"]
      tests:              ["tests/**/*", "**/test_*.py"]
      runtime:            ["src/**/*.py"]
      security_sensitive:
        - "src/auth/**/*.py"
        - "src/coordinare/services/github.py"
        - "**/migrations/**"
    forced_full_on_path_classes:                   # FR-005 deterministic overrides
      security: ["security_sensitive"]
      reviewer: ["security_sensitive"]
    classifier_latency_budget_seconds: 30.0        # FR-016
    classifier_failure_warning_cooldown_seconds: 600.0   # FR-006 rate limit

  personas:                                        # extends existing PersonaConfig
    reviewer:
      instructions: "..."
      scope_behavior:                              # 074: per-persona (opt-in; FR-010)
        skim:   { max_tool_calls: 5,  prompt_addon: "Spot-check correctness only..." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 30, prompt_addon: "Check invariants and edge cases." }
    security:
      instructions: "..."
      scope_behavior:
        skim:   { max_tool_calls: 5,  prompt_addon: "Sanity check; flag any risk you see." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 40, prompt_addon: "Threat-model the change." }
    qa:
      instructions: "..."
      scope_behavior:
        skim:   { max_tool_calls: 3,  prompt_addon: "Smoke-test only." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 40, prompt_addon: "Full regression sweep on affected areas." }
    tech_writer:
      instructions: "..."
      scope_behavior:
        skim:   { max_tool_calls: 3,  prompt_addon: "Verify docs are not stale." }
        normal: { max_tool_calls: 10, prompt_addon: "" }
        full:   { max_tool_calls: 25, prompt_addon: "Comprehensive docs review." }
    closer:
      instructions: "..."
      # NO scope_behavior — closer is scope-invariant (FR-009). If one is set, it is ignored with a startup warning.
```

## Pydantic schema

```python
class ScopeTierBehavior(BaseModel):
    # max_tool_calls bounds: ge=1 (zero would make the persona unable to act);
    # le=500 is a soft ceiling to catch runaway misconfigurations — well above the
    # ~40 ceiling used in shipped examples but below pathological values that would
    # exhaust a single cycle's budget.
    max_tool_calls: int | None = Field(default=None, ge=1, le=500)
    prompt_addon: str = Field(default="", max_length=4096)

class ScopeBehavior(BaseModel):
    skim:   ScopeTierBehavior | None = None
    normal: ScopeTierBehavior | None = None
    full:   ScopeTierBehavior | None = None
    model_config = ConfigDict(extra="forbid")

class PersonaConfig(BaseModel):                    # EXISTING — extended
    instructions: str = ""
    scope_behavior: ScopeBehavior | None = None    # 074

class PersonaScopeConfig(BaseModel):               # NEW
    enabled: bool = False
    path_classes: dict[str, list[str]] = Field(default_factory=dict)
    forced_full_on_path_classes: dict[str, list[str]] = Field(default_factory=dict)
    classifier_latency_budget_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    classifier_failure_warning_cooldown_seconds: float = Field(default=600.0, ge=0.0)

    @field_validator("path_classes")
    @classmethod
    def _validate_globs(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        # Reject empty class names and empty glob lists; trim whitespace
        ...

    @model_validator(mode="after")
    def _classes_in_forced_full_must_exist(self) -> "PersonaScopeConfig":
        defined = set(self.path_classes.keys())
        for persona, classes in self.forced_full_on_path_classes.items():
            unknown = set(classes) - defined
            if unknown:
                raise ValueError(
                    f"forced_full_on_path_classes[{persona!r}] references undefined classes: {sorted(unknown)}"
                )
        return self
```

## Dispatch wire format (FR-007 structured input)

When dispatching a persona, coordinare MUST attach the persona's scope slice to the dispatch payload as a structured field — NOT by string-interpolating into the persona's base prompt:

```python
dispatch_payload = {
    "card": {...},
    "persona": "reviewer",
    "scope": {                          # FR-007 structured input
        "depth": "skim",                # from PersonaScope.personas[persona].depth
        "focus": "Spot-check the new retry branch in session.refresh().",
        "max_tool_calls": 5,            # from persona.scope_behavior.<depth>.max_tool_calls (None if unset)
        "prompt_addon": "Focus on correctness only.",  # from persona.scope_behavior.<depth>.prompt_addon
        "overrides": ["forced_full_on_path_class:security_sensitive"],
    },
    # ...existing fields...
}
```

- `scope` is `None` when `persona_scope.enabled = false` OR when the persona has no `scope_behavior` block (FR-010 opt-out).
- For `closer`, `scope.depth` is always `"full"` and `scope.overrides` includes `"closer_is_scope_invariant"` (FR-009); only `focus` is intended to be consumed.
- Performers MUST treat `scope` as optional; absence means "behave as today."

## Validation rules

| Rule | Trigger | Behavior |
|---|---|---|
| `persona_scope.enabled = true` but `path_classes` empty | startup | startup warning; feature stays off (no classification) |
| `forced_full_on_path_classes` references undefined class | startup | hard fail (pydantic validation error) |
| A persona has `scope_behavior` but `persona_scope.enabled = false` | startup | startup info log; persona's `scope_behavior` ignored |
| `closer.scope_behavior` is set | startup | startup warning; ignored (FR-009) |
| Glob in `path_classes` is malformed | startup | hard fail |
| Per-persona `scope_behavior` missing one of `skim`/`normal`/`full` | runtime when that depth lands | persona falls back to today's behavior for that depth (no prompt_addon, no tool-call cap) — debug log |

## Backward compatibility

- Projects without a `persona_scope` block load as today; no behavior change.
- Projects with `persona_scope.enabled = true` but no per-persona `scope_behavior` blocks load with classifier running but no persona consuming its output — useful for shadow-mode rollout (operators can see classifier output in logs without changing persona behavior).
- Existing snapshots (v1–v3) load with `persona_scope = None` on every session and recompute on the next cycle (FR-011 graceful upgrade).
