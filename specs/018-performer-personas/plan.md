# Implementation Plan: Performer Personas

**Branch**: `018-performer-personas` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Add per-role behavioral persona support across the coordinare's eight AI-backed roles. Personas are plain-language instruction strings stored in the existing `config.yaml` under a `personas:` key. On each dispatch/assess/scan invocation the coordinare reads the effective instructions (custom or built-in default) and injects them into the payload or prompt context for that role's backend agent. The web dashboard exposes three new API endpoints for reading, updating, and resetting personas. No new dependencies are required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `pydantic>=2.9`, `pydantic-settings>=2.6`, `structlog>=24.1`, FastAPI (existing) — **no new dependencies required**
**Storage**: Existing `config.yaml` YAML file — extended with a `personas:` top-level key; no new persistence backend
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project — existing `src/coordinare/` layout
**Performance Goals**: Persona read adds < 1 ms per dispatch (in-memory Pydantic model read after config load); dashboard API responds in < 200 ms; hot-reload within one poll cycle (≤ 30 s)
**Constraints**: Persona instructions capped at 8,000 characters; no file-watching; config read on each invocation (hot-reload by re-reading `config.yaml` on demand for API writes); no DB required
**Scale/Scope**: 8 role slots; ~6 new Pydantic model fields, ~1 new service module (~80 LOC), ~4 modified dispatch/assess/scan nodes, ~3 new dashboard API endpoints, ~15–20 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `PersonaService` is single-responsibility; no cleverness; explicit default constants |
| II. Testing Discipline | PASS | Unit tests required for persona injection in every affected node, persona service, and all three API endpoints |
| III. User Experience | PASS | Dashboard shows "using defaults" indicator; reset action; errors are actionable |
| IV. Performance by Design | PASS | Persona read is in-memory (Pydantic model); no I/O added to hot path beyond existing config load |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers remain in spec |

All quality gates are expected to pass. No complexity violations.

## Project Structure

### Documentation (this feature)

```text
specs/018-performer-personas/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   └── personas-api.yaml
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (files changed)

```text
src/coordinare/
├── config.py                            # MODIFIED — add PersonaConfig, PersonasConfig, personas field
├── services/
│   └── persona_service.py               # NEW — get_effective_instructions, save_persona, reset_persona, DEFAULT_INSTRUCTIONS
├── graph/nodes/
│   ├── dispatch_card.py                 # MODIFIED — inject persona_instructions into card_context
│   └── assess_card.py                   # MODIFIED — inject persona_instructions into assessment details
├── services/
│   ├── agent_service.py                 # MODIFIED — read persona_instructions from card_context, add to payload
│   ├── assessment.py                    # MODIFIED — use persona_instructions in prompt building
│   └── advocate.py                      # MODIFIED — accept persona_instructions in scan context
└── dashboard.py                         # MODIFIED — add /api/personas GET/PUT/DELETE + dashboard UI section

tests/unit/
├── test_persona_service.py              # NEW — unit tests for PersonaService
├── test_config.py                       # MODIFIED — persona config parsing tests
├── graph/nodes/test_dispatch_card.py    # MODIFIED — persona injection tests
├── graph/nodes/test_assess_card.py      # MODIFIED — assessor persona injection tests
└── test_dashboard.py                    # MODIFIED — personas API endpoint tests
```

**Structure Decision**: No new files except `persona_service.py` and its test. All changes are targeted edits to existing files. The dashboard API endpoints are added to `dashboard.py` following the existing pattern for `/api/force-poll` and `/api/performer-logs`.

## Detailed Implementation Plan

### Step 1 — Extend Config Model (`src/coordinare/config.py`)

Add two new Pydantic models:

```python
class PersonaConfig(BaseModel):
    """Per-role behavioral instructions for the AI agent."""
    instructions: str = ""

class PersonasConfig(BaseModel):
    """Container for all eight role personas."""
    advocate: PersonaConfig = PersonaConfig()
    assessor: PersonaConfig = PersonaConfig()
    architect: PersonaConfig = PersonaConfig()
    implementer: PersonaConfig = PersonaConfig()
    reviewer: PersonaConfig = PersonaConfig()
    security: PersonaConfig = PersonaConfig()
    qa: PersonaConfig = PersonaConfig()
    tech_writer: PersonaConfig = PersonaConfig()
```

Add to `ProjectConfiguration`:

```python
personas: PersonasConfig = PersonasConfig()
```

**Config YAML example**:

```yaml
personas:
  implementer:
    instructions: "Always write tests first. Prefer functional patterns..."
  assessor:
    instructions: "Focus on business value and user impact..."
```

---

### Step 2 — Persona Service (`src/coordinare/services/persona_service.py`)

New module containing:

```python
PERSONA_MAX_LENGTH = 8_000  # characters

DEFAULT_INSTRUCTIONS: dict[str, str] = {
    "advocate": "Scan GitHub issues and identify the highest-value, well-specified items...",
    "assessor": "Evaluate whether the card has sufficient context for implementation...",
    "architect": "Analyse the codebase and produce a structured technical plan...",
    "implementer": "Implement the card according to the acceptance criteria and architecture plan...",
    "reviewer": "Review the implementation against the architecture plan and acceptance criteria...",
    "security": "Analyse the changes for OWASP Top 10 vulnerabilities, secret leakage, and insecure patterns...",
    "qa": "Validate that the implementation satisfies every acceptance criterion by running the test suite...",
    "tech_writer": "Produce a CHANGELOG entry, update README sections, and add inline documentation...",
}

VALID_ROLES = frozenset(DEFAULT_INSTRUCTIONS.keys())


def get_effective_instructions(role: str, personas: PersonasConfig) -> str:
    """Return the active persona instructions for a role.

    Falls back to DEFAULT_INSTRUCTIONS[role] when the configured instructions
    are absent, empty, or whitespace-only.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    persona: PersonaConfig = getattr(personas, role)
    instructions = (persona.instructions or "").strip()
    return instructions if instructions else DEFAULT_INSTRUCTIONS[role]


def save_persona(role: str, instructions: str, config_path: Path) -> None:
    """Write updated persona instructions to the YAML config file."""
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    if len(instructions) > PERSONA_MAX_LENGTH:
        raise ValueError(f"Instructions exceed maximum length ({PERSONA_MAX_LENGTH} chars)")
    # Read → modify → write back
    ...


def reset_persona(role: str, config_path: Path) -> None:
    """Clear persona instructions for a role, restoring built-in defaults."""
    save_persona(role, "", config_path)
```

---

### Step 3 — Inject into Dispatch Payload (`dispatch_card.py` + `agent_service.py`)

**dispatch_card.py** — before calling `agent.dispatch_card()`:

```python
from coordinare.services.persona_service import get_effective_instructions
instructions = get_effective_instructions("implementer", state["config"].personas)
card_context = {**card, "persona_instructions": instructions}
result = await agent.dispatch_card(card_context, workspace_info=workspace_info)
```

**agent_service.py** — in `dispatch_card()` payload builder:

```python
if card_context.get("persona_instructions"):
    payload["persona_instructions"] = card_context["persona_instructions"]
```

The performer receives `persona_instructions` as a top-level field in the dispatch payload alongside `title`, `description`, etc.

---

### Step 4 — Inject into Assessment Context (`assess_card.py` + `assessment.py`)

**assess_card.py** — before calling `backend.assess(details)`:

```python
from coordinare.services.persona_service import get_effective_instructions
details["persona_instructions"] = get_effective_instructions("assessor", state["config"].personas)
result = await backend.assess(details)
```

**assessment.py** — in `_build_assess_prompt()`:

```python
instructions = card.get("persona_instructions", "")
if instructions:
    prompt = f"## Assessor Instructions\n{instructions}\n\n" + prompt
```

---

### Step 5 — Inject into Advocate Context (`advocate.py` + `advocate_service.py`)

**advocate.py** — before calling `advocate_service.scan_and_respond()`:

```python
from coordinare.services.persona_service import get_effective_instructions
instructions = get_effective_instructions("advocate", state["config"].personas)
result = await advocate_service.scan_and_respond(processed_ids, persona_instructions=instructions)
```

**advocate_service.py** — pass instructions through to `ClaudeScorer`:

```python
async def scan_and_respond(self, processed_ids, *, persona_instructions: str = "") -> set:
    ...
    # Pass persona_instructions into scorer context
```

---

### Step 6 — Dashboard API Endpoints (`dashboard.py`)

Three new endpoints following the existing pattern:

```python
@app.get("/api/personas")
async def get_personas() -> list[dict]:
    """Return effective instructions and is_default flag for all roles."""

@app.put("/api/personas/{role}")
async def update_persona(role: str, body: dict) -> dict:
    """Update instructions for a role. Validates length and role name."""

@app.delete("/api/personas/{role}", status_code=204)
async def reset_persona_endpoint(role: str) -> None:
    """Reset a role's persona to built-in defaults."""
```

**Dashboard UI**: Add a collapsible personas panel in the existing single-page HTML. Each role shows its current instructions (or "Using default instructions" if not configured) with an inline textarea and Save/Reset buttons.

---

## Complexity Tracking

No constitution violations. All changes are targeted edits. No new packages required.

| Change | Scope | Justification |
|--------|-------|---------------|
| New `persona_service.py` module | 1 module, ~80 LOC | Central service prevents duplicate persona-reading logic across 3+ call sites |
