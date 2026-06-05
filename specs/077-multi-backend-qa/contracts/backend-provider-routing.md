# Contract — Backend Provider Routing & Terminal Outcome

Defines the contracts new/changed backends MUST honor. Contract tests assert
these using the producers' real env/output (076 T170 lesson).

## C-1: Backend factory registration

- `get_backend("pi")` MUST return a `PiBackend` instance (registered in `supported_backends`).
- `get_backend(<unknown>)` MUST raise `UnsupportedBackendError` naming the supported set (FR-007).
- Lazy import preserved: importing a backend with a missing CLI dependency must not break unrelated backends.

**Field Registry**

| Field | Where | Meaning |
|---|---|---|
| name | factory key + `BACKEND` env | backend identifier (`pi`, `openclaw`, …) |
| module | factory value | dotted adapter module path |
| class | factory value | adapter class implementing `BackendAdapter` |

## C-2: Pi provider routing (NEW)

When `PI_PROVIDER_BASE_URL` is set, `PiBackend` MUST drive the model through that
OpenAI-compatible endpoint (the LiteLLM proxy), authenticating with the key named
by `PI_PROVIDER_ENV_KEY` (default falls back to the standard OpenAI key var).

**Field Registry**

| Field | Required | Meaning |
|---|---|---|
| PI_PROVIDER_BASE_URL | presence activates override | OpenAI-compatible base URL (LiteLLM) |
| PI_PROVIDER_ENV_KEY | optional | env var holding the API key (default standard OpenAI key) |
| PI_PROVIDER_NAME | optional | provider id, if the Pi CLI needs a named provider |
| PI_PROVIDER_WIRE_API | optional | `chat` \| `responses`, if Pi distinguishes |

- MUST NOT use Pi-hosted models when the override is set (FR-002).
- Model comes from `performers.closer.model` (= `spark/qwen3.6:35b`), not hardcoded.

## C-3: OpenClaw provider routing (NEW)

When `OPENCLAW_PROVIDER_BASE_URL` is set, `OpenClawBackend` MUST drive the model
through that OpenAI-compatible endpoint (the LiteLLM proxy) by writing
`~/.openclaw/openclaw.json` (custom `openai-completions` provider + model
allowlist) and running `openclaw agent --local`, authenticating with the key
named by `OPENCLAW_PROVIDER_ENV_KEY`. It MUST NOT use a vendor-hosted model.

**Field Registry**

| Field | Required | Meaning |
|---|---|---|
| OPENCLAW_PROVIDER_BASE_URL | presence activates override | OpenAI-compatible base URL (LiteLLM) |
| OPENCLAW_PROVIDER_NAME | optional | provider id (default `litellm`); becomes the `<provider>/<model>` prefix |
| OPENCLAW_PROVIDER_ENV_KEY | optional | env var holding the API key |

## C-4: Terminal-outcome contract (all backends)

Every backend's final `BackendStatus.state` MUST map to a terminal outcome the
orchestrator parses:

| BackendStatus.state | Orchestrator outcome |
|---|---|
| `done` | DONE (or the partial_progress JSON sentinel → PARTIAL_PROGRESS) |
| `blocked` | BLOCKED (with `questions`) |
| `error` | error → existing retry/empty-output/idle handling (076) |

- A backend that emits a "remaining work" report WITHOUT the `partial_progress` sentinel MUST NOT be read as DONE (076 implementer-contract rule).
- Pi (closer role) MUST return a parseable terminal outcome — no unrecognized-output stall (SC-003).

## C-5: Shared-model invariant (observability)

- For every dispatched stage, captured LiteLLM traffic MUST show the request targeting `spark/qwen3.6:35b` (SC-002).
- The backend that ran each stage MUST be attributable from the performer container's `coordinare.*` labels + endpoint id / `BACKEND` env (FR-009).
