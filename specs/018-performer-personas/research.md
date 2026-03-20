# Research: Performer Personas

**Branch**: `018-performer-personas` | **Date**: 2026-03-18

## Decision 1: Persona Storage Backend

**Decision**: Store personas as a `personas:` top-level key in the existing `config.yaml` file using the existing `ProjectConfiguration` Pydantic-settings model.

**Rationale**:
- Config already uses YAML; operators already know where to edit it
- `ProjectConfiguration` is already loaded hot (each invocation re-reads via `from_yaml()`)
- No new persistence backend, no new dependency, no migration required
- Eight role slots fit naturally as nested YAML keys

**Alternatives considered**:
- Separate `personas.yaml` sidecar — adds file management complexity with no benefit at this scale
- Database table — massively over-engineered for 8 rows of text; no query requirements
- Environment variables — poor UX for multiline instruction text; no UI editability

---

## Decision 2: Hot-Reload Strategy

**Decision**: Read persona instructions from the `ProjectConfiguration` object on each dispatch/assess/scan invocation. The daemon does not cache the personas section at startup.

**Rationale**:
- The coordinare already reads `config.yaml` per-invocation for some paths; this is consistent
- No file-watching infrastructure needed (avoids inotify/polling complexity)
- Dashboard API writes to the YAML file and the next invocation automatically picks up the change
- Latency is negligible: YAML parse is < 1 ms for this file size

**Alternatives considered**:
- File watcher with in-memory cache — adds complexity (inotify, signal handling) for no meaningful latency benefit at this poll interval
- Daemon restart required — unacceptable per spec (SC-001 requires ≤ 30 s without restart)

---

## Decision 3: Payload Field for Implementer Persona

**Decision**: Inject persona instructions as `persona_instructions` (a string field) into the dispatch payload at the top level, alongside `title`, `description`, etc.

**Rationale**:
- The performer's wire protocol already passes arbitrary fields in the payload dict
- A top-level field is visible and accessible without nested traversal
- The performer passes the full payload to the AI backend as context — the instructions arrive exactly where they are needed
- Field name is unambiguous and consistent with other top-level payload fields

**Alternatives considered**:
- Nested under `context.persona` — adds unnecessary structure; all other context is flat
- Separate protocol action — over-engineered; no performer changes needed, instructions are just payload data

---

## Decision 4: Default Instructions Storage

**Decision**: Store default instructions as a module-level `DEFAULT_INSTRUCTIONS: dict[str, str]` constant in `persona_service.py`, not in the config file.

**Rationale**:
- Defaults are code, not user data — they belong in the codebase
- Keeps `config.yaml` clean (no boilerplate defaults cluttering the file)
- Makes it clear which instructions are "factory" vs user-configured
- Easy to update defaults via PR; no migration path needed

**Alternatives considered**:
- Store defaults in config.yaml alongside custom — defaults would appear in every config file, making diffs noisy and defaults editable (undesirable)
- Store defaults in a separate YAML template — adds a second file format to maintain

---

## Decision 5: Assessment Prompt Injection Pattern

**Decision**: Inject assessor persona instructions as a prefixed block at the top of the assessment prompt: `## Assessor Instructions\n{instructions}\n\n`.

**Rationale**:
- LLMs attend more strongly to content at the beginning of their context window
- Separating with a Markdown heading makes the instructions visually distinct from card content
- Consistent with how clarification history is already included in the prompt
- No changes to the `AssessmentBackend` protocol interface — instructions are part of the `card` dict

**Alternatives considered**:
- Append at end of prompt — less reliable attention; instructions may be ignored for long prompts
- New parameter on `backend.assess()` — would require changing the protocol interface and all backend implementations

---

## Decision 6: Advocate Persona Injection Point

**Decision**: Pass persona instructions to `AdvocateService.scan_and_respond()` as a keyword argument `persona_instructions: str = ""`, which is then forwarded to the `ClaudeScorer` as additional context.

**Rationale**:
- `scan_and_respond()` is the single entry point for all advocate behavior; centralising injection here avoids changes to multiple internal methods
- Keyword argument with default preserves backward compatibility (existing callers need not change)
- The ClaudeScorer is already the entity that interprets issue context — it is the right place for persona guidance

**Alternatives considered**:
- Pass directly to `ClaudeService` — would bypass the scorer abstraction and couple the config layer to the AI client
- Store on `AdvocateService` instance — would require service rebuild to change persona; breaks hot-reload guarantee

---

## Decision 7: Dashboard API Design

**Decision**: Three REST endpoints on the existing dashboard FastAPI app:
- `GET /api/personas` — list all roles with effective instructions and is_default flag
- `PUT /api/personas/{role}` — update one role's instructions
- `DELETE /api/personas/{role}` — reset one role to defaults (equivalent to PUT with empty string)

**Rationale**:
- REST pattern matches existing `/api/force-poll` endpoint style
- No GraphQL or WebSocket needed for a simple CRUD operation
- `DELETE` for reset is semantically correct (remove the customization)
- Existing dashboard server already has FastAPI; no new server needed

**Alternatives considered**:
- Single PATCH endpoint — less discoverable; harder to reset individual roles
- WebSocket for live persona editing — overkill; SSE already handles state push; personas change rarely
