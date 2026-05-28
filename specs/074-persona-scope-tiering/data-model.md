# Phase 1 Data Model: Persona Scope Tiering

## Runtime entities

### `PersonaScope`

Per-card classification result. Lives on `CardSession`; persists in v4 snapshots; absent in v1–v3 (treated as "not computed").

```python
from typing import Literal, TypedDict

Depth = Literal["skim", "normal", "full", "skip"]

class PersonaScopeSlice(TypedDict):
    depth: Depth
    focus: str                  # free-text, 1-3 sentences per FR-013
    overrides: list[str]        # structured override reasons, e.g. ["forced_full_on_path_class:security_sensitive"]

class PersonaScope(TypedDict):
    computed_at: str            # ISO-8601 UTC
    cycle_index: int            # monotonic, taken from the session
    classifier_model: str       # for audit; e.g. "anthropic_api:claude-haiku-4-5"
    head_sha: str               # PR HEAD at classification time, for the "did the diff drift?" check
    files_summary: list[dict]   # the deterministic input (audit trail)
    personas: dict[str, PersonaScopeSlice]   # persona_name -> slice; missing persona = "not constrained" = full
```

**Lifecycle**:
1. Created by `classify_scope` node on each cycle.
2. Read by `dispatch_performer` and `routing.py`.
3. Carried on `CardSession.persona_scope`; round-trips through `_SESSION_FIELDS`.
4. Serialized to disk as part of `PersistedSession.persona_scope` (optional; default `None`).
5. Surfaced on the PR via the `notify` node's rollup comment.

### `ClassificationFailure`

Sentinel state when classifier cannot produce a valid `PersonaScope`. Not a stored entity — surfaced through a `None`-valued `current_persona_scope` *or* (when a previous-cycle scope exists) by logging the failure and reusing the previous cycle's scope per FR-006 fallback hierarchy.

## Configuration entities

### `PersonaScopeConfig` (new — symphony-level)

```python
class PersonaScopeConfig(BaseModel):
    """074: per-card persona scope classification.

    When this block is absent OR when no persona has a `scope_behavior` block,
    the feature is fully opt-out (FR-010 additive default).
    """

    enabled: bool = False
    path_classes: dict[str, list[str]] = Field(default_factory=dict)
    """Project-defined: class_name -> list of file globs. e.g. {"docs": ["*.md"], "security_sensitive": ["src/auth/**/*.py"]}"""

    forced_full_on_path_classes: dict[str, list[str]] = Field(default_factory=dict)
    """Per-persona forced-full overrides: persona_name -> list of path_class names. FR-005."""

    classifier_latency_budget_seconds: float = Field(default=30.0, ge=1.0, le=600.0)  # FR-016
    classifier_failure_warning_cooldown_seconds: float = Field(default=600.0, ge=0.0)  # FR-006 rate limit

    @field_validator("forced_full_on_path_classes")
    @classmethod
    def _classes_must_exist(cls, v, info):
        # cross-validate against path_classes at config-load time;
        # a forced override referencing an undefined class is an error
        ...
```

### `ScopeBehavior` (new — per-persona)

Lives under `PersonaConfig.scope_behavior` in coordinare config (extends the existing class at `src/coordinare/config.py:159`).

```python
class ScopeTierBehavior(BaseModel):
    max_tool_calls: int | None = Field(default=None, ge=1, le=500)
    prompt_addon: str = ""   # appended to the persona invocation as a STRUCTURED INPUT, not a base-prompt mutation (FR-007)

class ScopeBehavior(BaseModel):
    skim:   ScopeTierBehavior | None = None
    normal: ScopeTierBehavior | None = None
    full:   ScopeTierBehavior | None = None
    # depth=skip is structural — no behavior block needed (the persona doesn't run at all)

class PersonaConfig(BaseModel):
    instructions: str = ""
    scope_behavior: ScopeBehavior | None = None   # 074: opt-in (FR-010)
```

### Where it plugs into the existing config tree

```yaml
symphony:
  personas:
    reviewer:
      instructions: "..."
      scope_behavior:                              # 074: per-persona, opt-in
        skim:   { max_tool_calls: 5,  prompt_addon: "Focus on correctness only..." }
        normal: { max_tool_calls: 15, prompt_addon: "Standard review." }
        full:   { max_tool_calls: 30, prompt_addon: "Deep review; check invariants." }
    security:
      instructions: "..."
      scope_behavior:
        skim:   { max_tool_calls: 5,  prompt_addon: "Sanity check; flag risk." }
        normal: { max_tool_calls: 15, prompt_addon: "Standard security review." }
        full:   { max_tool_calls: 40, prompt_addon: "Threat model the change." }
    closer:
      instructions: "..."
      # NOTE: closer ignores depth (FR-009); only `focus` is consumed.

persona_scope:                                     # 074: symphony-level, opt-in
  enabled: true
  path_classes:
    docs:              ["*.md", "docs/**/*"]
    config:            ["config/**/*.yaml", "*.toml"]
    tests:             ["tests/**/*", "**/test_*.py"]
    runtime:           ["src/**/*.py"]
    security_sensitive:
      - "src/auth/**/*.py"
      - "src/coordinare/services/github.py"
      - "**/migrations/**"
  forced_full_on_path_classes:
    security: ["security_sensitive"]
    reviewer: ["security_sensitive"]
  classifier_latency_budget_seconds: 30.0
```

## State entities (`CardSession` extension)

### `CardSession.persona_scope: PersonaScope | None`

New optional field on the existing `CardSession` TypedDict at `src/coordinare/session.py:28`.

**Persistence requirements (FR-011 — regression-prone)**:
- MUST be added to `_SESSION_FIELDS` at `src/coordinare/session.py:109`.
- MUST be initialized to `None` in `create_session_from_card()`.
- MUST round-trip through `session_to_state` / `state_to_session`.
- Regression test in `tests/unit/test_session.py` — same shape as the four existing round-trip regression tests (`test_last_blocked_slack_delivered_at_round_trips`, `test_lifecycle_completed_at_round_trips`, etc.).

### `PersistedSession.persona_scope: PersonaScope | None` (new — optional)

In `src/coordinare/state_store.py`. Defaults to `None`. v1–v3 snapshots load with `None` and trigger a recompute on the next cycle.

**Schema bump**: `CURRENT_SCHEMA_VERSION` 3 → 4. `MIN_SUPPORTED_SCHEMA_VERSION` unchanged at 1.

## Derived data

### Path-class membership of a file

```python
def classify_paths(files: list[FileChange], path_classes: dict[str, list[str]]) -> dict[str, list[str]]:
    """Returns: { file_path: [class_name, ...] } — one file may belong to many classes."""
    ...
```

Used by:
- The classifier prompt (as deterministic context — see `contracts/classifier-prompt.md`)
- The forced-full override evaluator (post-classifier; per FR-005)

## Snapshot persistence shape (JSON contract)

```json
{
  "schema_version": 4,
  "sessions": [
    {
      "card_id": "PVI_42",
      "...": "(existing fields)",
      "persona_scope": {
        "computed_at": "2026-05-27T18:30:00Z",
        "cycle_index": 7,
        "classifier_model": "anthropic_api:claude-haiku-4-5",
        "head_sha": "abc123...",
        "files_summary": [...],
        "personas": {
          "reviewer": { "depth": "skim", "focus": "...", "overrides": [] },
          "security": { "depth": "skip",  "focus": "no security-relevant paths touched", "overrides": [] },
          "qa":       { "depth": "skip",  "focus": "no runtime behavior changed", "overrides": [] },
          "tech_writer": { "depth": "normal", "focus": "...", "overrides": [] },
          "closer":   { "depth": "full", "focus": "...", "overrides": ["closer_is_scope_invariant"] }
        }
      }
    }
  ]
}
```

`persona_scope` is omitted (or `null`) when not yet computed; consumers MUST treat absent as "recompute next cycle."
