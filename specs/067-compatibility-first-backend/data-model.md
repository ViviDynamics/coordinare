# Phase 1 — Data Model

067 introduces no new persistent entities. It defines two **wire-format** shapes: the LCD request payload sent from the performer to the model endpoint, and the `UpstreamHTTPError` envelope returned from the performer to the coordinare when the model endpoint returns a non-2xx response.

---

## 1. LCD Chat-Completions Request Payload

**Producer**: `agent/performer/src/performer/backends/opencode_compat.py`
**Consumer**: any OpenAI-compatible `/v1/chat/completions` endpoint (OpenAI hosted, LM Studio, vLLM, Ollama, LiteLLM proxy).

### Allowed fields (LCD whitelist)

| Field | Type | Notes |
|-------|------|-------|
| `model` | string | Operator-supplied via `config.yaml`. |
| `messages[]` | array | Each message has `role` ∈ {`system`, `user`, `assistant`, `tool`}. `developer` MUST be remapped to `system` per [R4](./research.md). |
| `messages[].role` | string | See above. |
| `messages[].content` | string \| array | Per OpenAI baseline. |
| `messages[].tool_calls[]` | array (assistant) | Per OpenAI baseline. |
| `messages[].tool_call_id` | string (tool) | Per OpenAI baseline. |
| `tools[]` | array | Each entry MUST have `type: "function"`. No other type permitted. |
| `tools[].function.name` | string | Required. |
| `tools[].function.description` | string | Required. |
| `tools[].function.parameters` | object | JSON Schema (draft-07 subset). |
| `tool_choice` | string \| object | `"auto"`, `"none"`, or `{type: "function", function: {name: ...}}`. |
| `temperature` | number | Optional. |
| `max_tokens` | integer | Optional. |
| `stream` | boolean | Optional. |
| `response_format` | object | Only `{type: "json_object"}` is portable. |

### Forbidden fields (denied by validation before send)

| Field | Reason |
|-------|--------|
| `tools[].type != "function"` | Hosted-tool descriptors (`web_search`, `file_search`) — only OpenAI hosted endpoint executes these. |
| `messages[].role == "developer"` | Non-portable; remapped to `system` per R4. |
| `prompt_cache_key` | OpenAI-only field. |
| `response_format.json_schema` | OpenAI-only extension; not implemented by LM Studio / Ollama. |
| `parallel_tool_calls` | Inconsistent support across local servers. |
| `user` (telemetry tag) | OpenAI-only; some local servers reject unknown top-level fields. |

### Validation rule

The adapter MUST run an outbound validator (`_assert_lcd_payload(body)`) immediately before HTTP send. Any violation raises `LcdPayloadError` (a programming error — the prompt or tool registry is wrong, not the operator). Unit tests assert each forbidden field is caught.

---

## 2. UpstreamHTTPError Envelope

**Producer**: performer (`http_performer_service` caller path, raised from `opencode_compat.py` when the model endpoint returns non-2xx).
**Consumer**: coordinare — `src/coordinare/services/http_performer_service.py` deserializes; `src/coordinare/graph/nodes/handle_system_error.py` classifies.

### Schema (pydantic v2)

```python
class UpstreamHTTPError(BaseModel):
    kind: Literal["upstream_http_error"] = "upstream_http_error"
    status: int                          # raw HTTP status from upstream
    body: str                            # upstream response body, UTF-8, truncated to BODY_CAP_BYTES
    body_truncated: bool                 # True if body was truncated
    route: str                           # e.g. "POST /v1/chat/completions"
    base_url: str                        # endpoint base URL with credentials stripped
    upstream_request_id: str | None = None  # optional: passed through if upstream emits one
    elapsed_ms: int                      # time from request send to response received
    occurred_at: datetime                # UTC timestamp
```

**Constants**:
- `BODY_CAP_BYTES = 2048` — FR-004's "known cap (proposed 2 KiB)" pinned.
- `TRUNCATION_SUFFIX = "...<truncated>"` — appended when truncated, included in the 2048 cap.

### Transient/Permanent classification (FR-005)

Single source of truth in `src/coordinare/graph/nodes/handle_system_error.py`:

```python
TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
TRANSIENT_BODY_MARKERS: tuple[str, ...] = ("temporarily unavailable", "rate limit")
```

- Status in `TRANSIENT_STATUSES` → transient (retry with `github_retry`-style backoff).
- Otherwise → permanent (log + surface to dashboard).
- The body-marker tuple is a documented fallback for endpoints whose status is **not** in `TRANSIENT_STATUSES` but whose body indicates a transient condition (e.g. some LiteLLM proxies return 500 with `"upstream temporarily unavailable"`; some return 400 with `"rate limit"`). It only runs against `UpstreamHTTPError` instances — i.e. non-2xx upstream responses. 2xx responses are never reclassified. It is **the** authoritative list; no other substring-matching of upstream content is permitted anywhere else in the codebase.

### Log emission (FR-004)

When the coordinare logs an `UpstreamHTTPError`, it MUST emit a single structlog line at WARN (transient) or ERROR (permanent) containing all envelope fields. The body is logged verbatim (already truncated upstream) — no wrappers, no friendly-rephrasing.

Field ordering in the log line (structlog kv pairs): `status`, `route`, `base_url`, `upstream_body`, `body_truncated`, `elapsed_ms`, `upstream_request_id`.

---

## 3. Configuration surface

`config.example.yaml` gains:

```yaml
performers:
  implementer:
    backend: opencode_compat     # new value; opencode and codex still supported
    base_url: http://localhost:1234/v1    # LM Studio default; OpenAI works too
    model: qwen3-coder-30b
    # Optional safety net for prompts that still use `developer` role:
    compat_remap_developer_role: true     # default true; see specs/067/research.md R4
```

No new top-level config keys at the coordinare level. All new fields live under the existing `performers.<role>` block.

**Transport**: `compat_remap_developer_role` is read by the performer adapter directly from its loaded `config.yaml` at process start. It is **not** dispatched per-card; coordinare neither reads nor forwards it. No entry in `specs/contracts/dispatch-payload.md` is required.

**Endpoint sizing constraint (operator concern, not adapter-enforced)**: the model loaded behind `base_url` must have `n_ctx` large enough to hold the env_bootstrap prompt + tool-call history (32 KiB is the tested reference). The adapter does not check this; failures surface as `status: 400` with `n_keep >= n_ctx` in the body and are operator-fixable per `quickstart.md` troubleshooting.

---

## State transitions

None. 067 changes only the wire layer. Coordinare LangGraph topology, performer session lifecycle, and card-state machine are all unmodified.
