# Data Model — 077 Diverse Multi-Backend QA Round

This round adds no persisted schema (no `state_store` / snapshot changes). The
"entities" here are configuration- and protocol-level structures.

## 1. Backend registration entry

A mapping from a backend name to its adapter class, in the performer factory
(`get_backend`'s `supported_backends`).

| Field | Type | Notes |
|---|---|---|
| name | str | `BACKEND` env / `performers.<role>.backend` value (e.g. `pi`, `openclaw`, `opencode`) |
| module | str | dotted module path (e.g. `performer.backends.pi`) |
| class | str | adapter class implementing `BackendAdapter` (e.g. `PiBackend`) |

**Validation**: an unknown name MUST raise `UnsupportedBackendError` (FR-007).
This round adds one entry: `"pi" → ("performer.backends.pi", "PiBackend")`.

## 2. BackendAdapter (protocol — unchanged)

The contract `PiBackend` and `OpenClawBackend` implement
(`agent/performer/src/performer/backends/base.py`). Reproduced for reference:

- `async start(stand, score, *, model, effort, temperature, max_tokens) -> None`
- `get_status() -> BackendStatus` — `state ∈ {working, blocked, done, error}`, plus `questions`, `error_reason`, `stop_reason`, `output`
- `drain_events() -> list[BackendEvent]`
- `async relay_feedback(feedback) -> None`
- `async stop() -> None`

**Terminal-contract mapping** (how `BackendStatus` → orchestrator outcome): a
backend's final state maps to DONE / PARTIAL_PROGRESS / BLOCKED exactly as the
existing backends do (the implementer persona's `partial_progress` JSON sentinel
and the `blocked`/`error` states). Pi MUST honor this so the closer's outcome is
parsed, not stalled (FR-003, SC-003).

## 3. Provider-routing env contract (per backend)

The env vars a backend reads to reach the LiteLLM proxy + shared model. All
route to `spark/qwen3.6:35b`; the *model* comes from `performers.<role>.model`.

| Backend | Routing env (→ LiteLLM) | Status |
|---|---|---|
| codex | `CODEX_PROVIDER_BASE_URL`, `CODEX_PROVIDER_NAME`, `CODEX_PROVIDER_ENV_KEY`, `CODEX_PROVIDER_WIRE_API` | existing |
| claude_code | `LITELLM_PROXY_BASE_URL`, `LITELLM_PROXY_AUTH_TOKEN` (shim) | existing |
| hermes | `HERMES_PROVIDER`, `HERMES_BASE_URL`, `HERMES_API_KEY`, `HERMES_MODEL` | existing |
| junie | `JUNIE_PROVIDER_BASE_URL`, `JUNIE_PROVIDER_MODEL_ID`, `JUNIE_PROVIDER_MODEL`, `JUNIE_PROVIDER_API_TYPE`, `JUNIE_PROVIDER_API_KEY_ENV` | existing |
| opencode | provider keys via mounted `~/.opencode/config.json` | existing (creds needed) |
| **pi** | **`PI_PROVIDER_BASE_URL`, `PI_PROVIDER_ENV_KEY`** (+ optional `PI_PROVIDER_NAME`/wire-api as Pi requires) | **NEW (R-01)** |
| **openclaw** | **`OPENCLAW_PROVIDER_BASE_URL`, `OPENCLAW_PROVIDER_NAME`, `OPENCLAW_PROVIDER_ENV_KEY`** | **NEW (R-07)** |

**Invariant (FR-002)**: when a provider override is configured, the backend MUST
target the proxy + shared model and MUST NOT silently fall back to a vendor-hosted
model. Verifiable from captured LiteLLM traffic (SC-002).

## 4. Role → backend mapping (the round)

The per-stage assignment within the `website` symphony.

| Role / stage | Backend | Endpoint |
|---|---|---|
| assessing | junie | junie-ephemeral |
| architecting | codex | codex-ephemeral |
| implementing | codex | codex-ephemeral |
| security | codex | codex-ephemeral |
| reviewing | openclaw | openclaw-ephemeral |
| qa | claude_code | claude-ephemeral |
| documenting | hermes | hermes-ephemeral |
| env_bootstrap | opencode | opencode-ephemeral |
| closing_review | **pi** | **pi-ephemeral** |

## 5. Per-backend finding

A recorded observation from the live run (FR-010 / US5).

| Field | Type | Notes |
|---|---|---|
| backend | str | which backend |
| role | str | which lifecycle stage it ran |
| verdict | enum | `contract-respecting` \| `needs-fix` |
| evidence | str | log events / branch artifacts / captured LiteLLM traffic |
| follow_up | str? | task/commit raised, if any |

**Stage-pass rule (clarification)**: verdict is `contract-respecting` when the
backend ran, drove the shared model, and returned a valid terminal contract —
regardless of whether the card merged. Model-quality issues are findings, not
backend failures.
