# Contract: Config Schema (`persona_scope.persona_check_map` + `persona_scope.ci_gate`)

**Branch**: `075-implementer-ci-gate` | **Plan**: [../plan.md](../plan.md) | **Data model**: [../data-model.md](../data-model.md)

Pinned schema for the operator-facing config additions. Enforced by `tests/contract/test_persona_check_map_schema.py`.

---

## 1. Location in the config tree

```yaml
symphony:
  persona_scope:        # existing 074 block
    enabled: true
    persona_check_map:  # NEW (075) — optional
      <persona_name>:
        skim:   [<glob>, ...]
        normal: [<glob>, ...]
        full:   [<glob>, ...]
    ci_gate:            # NEW (075) — optional, default-off
      enabled: true|false
      max_bounces_per_head: <int 1..20>
      pending_timeout_seconds: <int 60..7200>
```

Nesting under `persona_scope` rather than a new top-level key keeps the surface coherent: gate config is meaningful only when scope-aware required-check resolution is in play, and operators who don't run 074 fall through to branch-protection / all-checks resolution automatically.

---

## 2. `PersonaCheckMapConfig` schema

```python
class PersonaCheckMapPerDepth(BaseModel):
    skim:   list[str] = Field(default_factory=list)
    normal: list[str] = Field(default_factory=list)
    full:   list[str] = Field(default_factory=list)

class PersonaCheckMapConfig(RootModel[dict[str, PersonaCheckMapPerDepth]]):
    pass
```

**Validation rules**:

- Top-level keys are persona names (`implementer`, `reviewer`, etc.). No validation against a fixed enum — operators may register custom personas.
- Each persona's value is a `PersonaCheckMapPerDepth`. Missing depth keys default to empty list (no required checks for that depth).
- Patterns are shell-style globs (`fnmatch.fnmatch`), case-sensitive. Empty pattern lists mean "no required checks via this layer" — resolver falls through to next layer.
- A persona absent from the map means "no per-persona required checks" — resolver falls through.
- Depth `skip` is intentionally not in the schema — `skip` means the persona is not invoked, so gate evaluation never occurs for it.

**Example**:

```yaml
persona_check_map:
  implementer:
    skim:   ["lint*"]
    normal: ["lint*", "unit-tests*"]
    full:   ["lint*", "unit-tests*", "integration*", "e2e*"]
  reviewer:
    normal: ["lint*", "unit-tests*"]
```

---

## 3. `CIGateConfig` schema

```python
class CIGateConfig(BaseModel):
    enabled: bool = False
    max_bounces_per_head: int = Field(default=3, ge=1, le=20)
    pending_timeout_seconds: int = Field(default=900, ge=60, le=7200)
```

**Validation rules**:

- `enabled=False` (default) → gate is bypassed entirely; lifecycle advance behavior is identical to pre-075. Adoption is fully opt-in.
- `max_bounces_per_head ∈ [1, 20]`. Below 1 is meaningless (a single failure would escalate immediately); above 20 is almost certainly an operator error and out of spec.
- `pending_timeout_seconds ∈ [60, 7200]`. Delegated to 064's `decide(pending_timeout_seconds=...)` — same semantics: a check pending longer than this is treated as failed.

---

## 4. Defaults and backwards compatibility

| Config state | Behavior |
|---|---|
| `persona_scope` absent | Lifecycle unchanged; gate disabled. |
| `persona_scope.enabled: false` | Lifecycle unchanged; gate disabled. |
| `persona_scope.enabled: true`, no `ci_gate` block | 074 features active; gate disabled. |
| `persona_scope.enabled: true`, `ci_gate.enabled: false` | 074 features active; gate disabled. |
| `persona_scope.enabled: true`, `ci_gate.enabled: true`, no `persona_check_map` | Gate active; resolver falls through to branch-protection then all-checks. |
| Full config (all three keys) | Gate active; resolver uses `persona_check_map` first. |

This grid is enforced by integration tests, not just unit parses — operators should be able to enable features incrementally.

---

## 5. Error cases

| Input | Expected outcome |
|---|---|
| `max_bounces_per_head: 0` | `ValidationError` — below `ge=1` |
| `max_bounces_per_head: 100` | `ValidationError` — above `le=20` |
| `pending_timeout_seconds: 30` | `ValidationError` — below `ge=60` |
| `persona_check_map: "string"` | `ValidationError` — must be dict |
| `persona_check_map.implementer.skim: "lint"` | `ValidationError` — must be list of str |
| `persona_check_map.implementer.fast: [...]` | `ValidationError` — unknown depth (only skim/normal/full accepted) |
| `ci_gate: null` | Defaults to `CIGateConfig(enabled=False, ...)` — null treated as omitted |

Tests in `tests/contract/test_persona_check_map_schema.py` exercise each row.

---

## 6. Documentation surface

Operator-facing docs live in `quickstart.md` (Phase 1) — this contract is the source of truth for the schema; the quickstart shows the adoption path.
