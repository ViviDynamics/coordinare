# Contract: Config Catalogs & Resolution

Defines the load-time contract for the `endpoints` / `model_endpoints` / `modes` catalogs and the `performer.mode` reference. This is what coordinare's config validation MUST enforce. Contract tests live in `tests/contract/` and `tests/unit/config/`.

## Field Registry

| Field | Container | Type | Required | Resolves to |
|---|---|---|---|---|
| `endpoints[].name` | root | str | yes | (key) |
| `endpoints[].kind` | root | enum | yes | — |
| `endpoints[].base_url` | root | str | iff self-hosted kind | — |
| `endpoints[].auth_env` | root | str | optional | env var name |
| `model_endpoints[].name` | root | str | yes | (key) |
| `model_endpoints[].endpoint` | root | str | yes | `endpoints[].name` |
| `model_endpoints[].model` | root | str | yes | — |
| `modes[].name` | root | str | yes | (key) |
| `modes[].strategy` | root | enum | yes | — |
| `modes[].tool` | root | str | yes | `model_endpoints[].name` |
| `modes[].thinking` | root | str | iff strategy ≠ single | `model_endpoints[].name` |
| `modes[].classifier` | root | str | iff strategy = conditional | `model_endpoints[].name` |
| `modes[].threshold` | root | float | iff strategy = conditional | — |
| `modes[].invalidate_after_turns` | root | int | think_once only | — |
| `modes[].invalidate_on_error` | root | bool | think_once only | — |
| `modes[].error_pattern` | root | str | think_once only (optional; has default) | regex for FR-016 error marker |
| `modes[].expose_plan_as` | root | enum | optional (default `thinking`) | — |
| `modes[].on_think_error` | root | enum | optional (default `fall_back_to_act`) | — |
| `performers.<role>.mode` | performers | str | yes | `modes[].name` |
| `performers.<role>.model` | performers | — | **REMOVED** | load error (FR-006) |

## Validation rules (MUST)

1. **Reference resolution** — every `model_endpoints[].endpoint`, `modes[].{tool,thinking,classifier}`, and `performers.<role>.mode` resolves to a defined name. Dangling ref → load error naming the missing target. (FR-005)
2. **Hard cut** — any `performers.<role>.model` present → load error directing to the catalog. (FR-006)
3. **Endpoint kind** — native kind (`openai`/`anthropic`) MUST NOT set `base_url`; self-hosted kind (`litellm`/`ollama`/`vllm`) MUST set `base_url`. (FR-007)
4. **Strategy/field consistency** — `single` MUST NOT carry `thinking`/`classifier`/strategy params; `always`/`conditional`/`think_once` MUST set `thinking`; `conditional` MUST set `classifier` + `threshold`. (FR-008)
5. **Unknown fields** — `extra="forbid"` on all three catalog models.
6. **Unique names** — duplicate `name` within any catalog → load error.

## Example acceptance assertions

- A valid config with one `single` self-hosted mode loads and `resolved_role("reviewer")` returns backend + resolved tool model_endpoint with no proxy flag.
- A `single` mode with `thinking:` set → `ValidationError` mentioning `thinking`.
- A `modes[].tool` naming a nonexistent model_endpoint → `ValidationError` naming it.
- A performer with inline `model:` → `ValidationError` citing FR-006 migration.
- A native `kind: anthropic` endpoint with `base_url` → `ValidationError`.
