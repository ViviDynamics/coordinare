# Data Model: Performer Personas

**Branch**: `018-performer-personas` | **Date**: 2026-03-18

## Entities

### PersonaConfig

A single role's persona definition. Stored as a nested object within `PersonasConfig`.

| Field | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `instructions` | `str` | `""` | max 8,000 characters | Freeform behavioral instruction text for the AI agent. Empty or whitespace = use default. |

**Validation**: Instructions exceeding 8,000 characters MUST be rejected with a `ValueError` at write time (save_persona) and a `422` at the API layer.

---

### PersonasConfig

Container for all eight role personas. A field for every coordinare role.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `advocate` | `PersonaConfig` | `PersonaConfig()` | Advocate role persona |
| `assessor` | `PersonaConfig` | `PersonaConfig()` | Assessor role persona |
| `architect` | `PersonaConfig` | `PersonaConfig()` | Architect role persona |
| `implementer` | `PersonaConfig` | `PersonaConfig()` | Implementer role persona |
| `reviewer` | `PersonaConfig` | `PersonaConfig()` | Reviewer role persona |
| `security` | `PersonaConfig` | `PersonaConfig()` | Security performer persona |
| `qa` | `PersonaConfig` | `PersonaConfig()` | QA performer persona |
| `tech_writer` | `PersonaConfig` | `PersonaConfig()` | Tech writer persona |

**Note**: `PersonasConfig` is added as a field on the existing `ProjectConfiguration` model:
```
ProjectConfiguration.personas: PersonasConfig = PersonasConfig()
```

---

### DEFAULT_INSTRUCTIONS

A module-level constant dictionary in `persona_service.py` — not a database entity. Maps role names to their built-in default instruction strings.

```
DEFAULT_INSTRUCTIONS: dict[str, str] = {
    "advocate":     <built-in advocate instructions>,
    "assessor":     <built-in assessor instructions>,
    "architect":    <built-in architect instructions>,
    "implementer":  <built-in implementer instructions>,
    "reviewer":     <built-in reviewer instructions>,
    "security":     <built-in security instructions>,
    "qa":           <built-in QA instructions>,
    "tech_writer":  <built-in tech writer instructions>,
}
```

---

### PersonaResponse (API response shape)

The shape returned by the dashboard API for each role. Not stored; derived at request time.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `str` | One of the eight valid role names |
| `instructions` | `str` | The effective instructions (custom or default) |
| `is_default` | `bool` | `True` when instructions are the built-in default (no customization active) |

---

## Config YAML Schema

After this feature, the `config.yaml` supports a top-level `personas:` key:

```yaml
# config.yaml (excerpt — all fields are optional; unset = use defaults)
personas:
  implementer:
    instructions: |
      Always write tests first using TDD.
      Prefer functional patterns over stateful classes.
      Add structured logging at DEBUG level for all non-trivial operations.
  assessor:
    instructions: |
      Focus on business value and user impact.
      Only ask questions when the acceptance criteria are genuinely ambiguous.
  advocate:
    instructions: |
      Prioritise issues labelled 'coordinare-ready'.
      Ignore issues tagged 'backlog'.
```

Roles absent from the YAML use built-in defaults automatically.

---

## State Transitions

Personas have no state machine — they are configuration, not runtime state. The relevant transition is:

```
[No config] ──save_persona()──▶ [Custom instructions active]
[Custom]    ──reset_persona()──▶ [No config / defaults active]
```

The `is_default` flag in `PersonaResponse` reflects the current position in this binary state.

---

## Relationships

```
ProjectConfiguration
    └── personas: PersonasConfig
            ├── advocate: PersonaConfig
            ├── assessor: PersonaConfig
            ├── architect: PersonaConfig
            ├── implementer: PersonaConfig
            ├── reviewer: PersonaConfig
            ├── security: PersonaConfig
            ├── qa: PersonaConfig
            └── tech_writer: PersonaConfig

PersonaService (stateless)
    ├── reads PersonasConfig → returns effective instructions
    ├── writes to config.yaml → updates PersonaConfig.instructions
    └── references DEFAULT_INSTRUCTIONS for fallback
```
