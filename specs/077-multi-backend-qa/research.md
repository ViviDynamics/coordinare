# Research — 077 Diverse Multi-Backend QA Round

Phase 0 decisions. All `NEEDS CLARIFICATION` items were resolved in
`/speckit.clarify` (Pi routing = OpenAI-compat via LiteLLM; success bar = full
mapping; stage-pass = backend-correctness).

## R-01: Pi backend adapter shape

- **Decision**: Implement `PiBackend` in `agent/performer/src/performer/backends/pi.py` to the existing `BackendAdapter` Protocol (`start`, `get_status`, `drain_events`, `relay_feedback`, `stop`), modeled directly on `CodexBackend`. It invokes the Pi CLI per turn and reaches the model via an **OpenAI-compatible provider override** (`PI_PROVIDER_BASE_URL` → the LiteLLM proxy, `PI_PROVIDER_ENV_KEY` → `LITELLM_MASTER_KEY`), driving `spark/qwen3.6:35b`. Register `"pi": ("performer.backends.pi", "PiBackend")` in `get_backend`'s `supported_backends` dict.
- **Rationale**: The clarification fixed Pi as OpenAI-compatible-via-LiteLLM, which is exactly codex's/hermes's routing model. Reusing the codex pattern (write a provider config, launch the CLI pointed at it, parse terminal output into `BackendStatus`) minimizes new surface and keeps Pi *beneath* the backend facade (interface-first guidance). The factory's lazy import means a missing Pi CLI only affects Pi-configured runs.
- **Alternatives considered**:
  - Pi via its own hosted backend / custom-model override (junie-style) — rejected by clarification (must not use Pi-hosted models; keep the one-shared-model invariant).
  - A generic "openai_compat CLI" backend instead of a named `pi` — rejected: the round explicitly wants a distinctly-named Pi backend for per-backend attribution in findings.
- **Spike findings (T010, 2026-05-29 — pi.dev docs):** Pi is a terminal coding harness with a non-interactive mode and OpenAI-compatible custom providers — **feasible** for the round:
  - Invocation: `pi -p "<prompt>"` (print/exit); `pi --mode json` emits JSON-lines events (parseable for `BackendStatus`); model/provider selected via `pi --model <id>` and/or `pi --provider <name>`.
  - Custom OpenAI-compatible provider via an **extension** `pi.registerProvider("litellm", { baseUrl: "<LiteLLM>/v1", apiKey: "$LITELLM_MASTER_KEY", api: "openai-completions", models: [{ id: "spark/qwen3.6:35b", contextWindow, maxTokens, ... }] })` — or the equivalent `models.json` (same `$ENV_VAR` / `${ENV_VAR}` / `!command` value syntax). So `PI_PROVIDER_BASE_URL`/`PI_PROVIDER_ENV_KEY` env vars map to writing this provider config at job start (analogous to codex writing `config.toml`).
  - Adapter (T014) shape confirmed close to `CodexBackend`: write the provider config, launch `pi -p --mode json --provider litellm --model spark/qwen3.6:35b`, parse the JSON-lines events into `BackendStatus`.
  - Remaining in-container confirmations (do during T014/T016): the exact JSON event schema, and that the `pi` CLI installs into `coordinare-performer:full`.
- **POC (2026-05-30 — proven end-to-end against the live LiteLLM proxy + spark/qwen3.6:35b):**
  - Install: `npm install -g @mariozechner/pi-coding-agent` into `coordinare-performer:full` (Node 22 already present). Pi 0.73.1. → T016 install method confirmed.
  - Provider config path is **`~/.pi/agent/models.json`** (NOT `$PI_HOME/...`): `{"providers":{"litellm":{"baseUrl":"<proxy>/v1","api":"openai-completions","apiKey":"${LITELLM_MASTER_KEY}","compat":{"supportsDeveloperRole":false,"supportsReasoningEffort":false},"models":[{"id":"spark/qwen3.6:35b"}]}}}`.
  - **`compat` block is REQUIRED** — qwen (OpenAI-compatible) rejects the `developer` role / `reasoning_effort` without it.
  - The built-in `openai` provider does NOT honor `OPENAI_BASE_URL` (it hit api.openai.com → 401); the custom-provider route is the correct one.
  - Run `pi -p --mode json --provider litellm --model spark/qwen3.6:35b "<task>"` returned `provider:litellm, model:spark/qwen3.6:35b, stopReason:stop` with the final assistant text (`pong`) in `agent_end.messages[]` — the exact stream `PiBackend` parses (it extracts the `text` part, ignoring `thinking`). 1493 tokens billed through the proxy.
  - `pi.py` corrected from this POC: config path → `~/.pi/agent/models.json`; added the `compat` block.

## R-02: ~~(withdrawn)~~

A backend candidate evaluated for this round had no custom OpenAI-compatible
provider / base-URL support (it routed only through a vendor-hosted gateway), so
it could not drive the shared self-hosted model (FR-002) and was removed from the
codebase. Number retained as a gap so R-03…R-07 stay stable.

## R-03: opencode for env_bootstrap

- **Decision**: Reuse `OpenCodeAdapter` unchanged; route the env_bootstrap role to an opencode endpoint. Document the `~/.opencode/config.json` creds path (read-only mount, as in `config.opencode.yaml`). Validate that the dev-env install completes and the `service_inference` probe timeout stays non-fatal (env cache still ready).
- **Rationale**: The opencode backend already exists and is proven for card roles; env_bootstrap is just another role dispatch. The non-fatal-inference behavior is already in `main` (076 T175) at the performer layer, so it applies to any backend including opencode — this round verifies it empirically under opencode rather than re-implementing.
- **Alternatives considered**:
  - Keep env_bootstrap on claude_code (proven) — rejected: the user chose opencode for bootstrap; validating a second backend on the bootstrap path is part of the round's coverage goal.
- **POC (2026-05-30 — proven end-to-end against live LiteLLM + spark/qwen3.6:35b):** opencode follows the pi pattern.
  - Install: entrypoint `opencode)` arm (curl) already present. opencode 1.15.12.
  - Config: write **`opencode.json`** (project dir) with a custom provider — the opencode analogue of pi's `models.json`:
    `{"$schema":"https://opencode.ai/config.json","provider":{"litellm":{"npm":"@ai-sdk/openai-compatible","name":"LiteLLM","options":{"baseURL":"<proxy>/v1","apiKey":"{env:LITELLM_MASTER_KEY}"},"models":{"spark/qwen3.6:35b":{}}}}}`. Use `@ai-sdk/openai-compatible` (/v1/chat/completions). apiKey via `{env:VAR}` syntax.
  - Run: `opencode run -m litellm/spark/qwen3.6:35b --format json "<task>"` → JSON events; returned `{"type":"text","text":"pong"}`.
  - **No `~/.opencode` login creds needed** — the provider config + LITELLM key is sufficient. This DISSOLVES the earlier US4 creds blocker.
  - **No `compat` block needed** (unlike pi) — the AI-SDK openai-compatible uses standard system/user roles, so qwen accepts it directly.
  - **Implication for US4 impl:** `OpenCodeAdapter` currently relies on a mounted `~/.opencode`; instead write `opencode.json` at job start from `OPENCODE_PROVIDER_*` env (mirror `PiBackend._write_provider_config`). The session-create already sends `modelID` — pass `litellm/spark/qwen3.6:35b`.
  - **LANDED (T025/T026):** `OpenCodeAdapter.start()` now writes `opencode.json` (opt-in on `OPENCODE_PROVIDER_BASE_URL`, the env prefix keyed off `adapter_name`) and prefixes `modelID = <provider>/<model>`. When the env is unset the mounted-creds path is untouched. `config.yaml` routes `env_bootstrap → opencode-ephemeral`. Unit-covered by `TestOpenCodeProviderRouting` (3 tests). Live per-stage validation = T028.

## R-04: FR-007 — unknown-backend rejection

- **Decision**: The performer factory (`get_backend`) already raises `UnsupportedBackendError` for unknown names. Research/confirm whether coordinare surfaces an unknown `performers.<role>.backend` (or endpoint `BACKEND`) **before dispatch** (config validation / startup) vs only inside the container at runtime; if it's runtime-only, add a startup/config-validation check enumerating the supported backend names so the operator gets a pre-dispatch error.
- **Rationale**: Failing loudly pre-dispatch (vs a silent in-container failure) satisfies FR-007 and Constitution III (actionable errors). Adding `"pi"` to the supported set is a one-line factory change; the validation check (if missing) is small and shares the backend-name list.
- **Alternatives considered**: Rely solely on the in-container `UnsupportedBackendError` — rejected: it surfaces late (after a container spins up) and reads as a dispatch failure rather than a config error.

## R-05: Per-stage backend observability (FR-009)

- **Decision**: Reuse the existing per-performer Docker labels (076 added `coordinare.performer_stage`, `coordinare.session_id`, etc.) plus the `performer_endpoint`/`BACKEND` mapping to attribute each stage to its backend in logs and the dashboard. No new telemetry needed — the endpoint id (e.g. `pi-ephemeral`) and `BACKEND` env already identify the backend per dispatch.
- **Rationale**: 076 already plumbed per-stage container labels; the round just needs to read them. Avoids new observability surface (Constitution IV).

## R-06: Findings report format (FR-010 / US5)

- **Decision**: Produce the per-backend findings as a Phase-style section in this spec's `tasks.md`/a `findings.md`, mirroring spec 076's "Phase 9 live-test fixes" format: per backend × role, a verdict (contract-respecting / needs-fix), evidence (log events, branch artifacts, captured LiteLLM traffic), and any follow-up task/commit.
- **Rationale**: 076's Phase 9 is the established, reviewable precedent for live-test findings in this project; reusing it keeps reporting consistent and PR-reviewable.
- **Alternatives considered**: GitHub issues per finding — heavier; defer to spinning off only the findings that warrant separate fixes.

## R-07: OpenClaw backend for the reviewer (US6, added mid-round)

- **Decision**: Implement `OpenClawBackend` in `agent/performer/src/performer/backends/openclaw.py` to the `BackendAdapter` Protocol, modeled on `HermesBackend`/`PiBackend` (one-shot embedded-agent CLI). Reach the model via a custom OpenAI-compatible provider written to `~/.openclaw/openclaw.json` from `OPENCLAW_PROVIDER_*` env, driving `spark/qwen3.6:35b` via LiteLLM. Register `"openclaw": ("performer.backends.openclaw", "OpenClawBackend")` in the factory; install via an entrypoint `openclaw)` arm (npm). Route the `reviewer` role to it (replacing claude_code on that stage).
- **Rationale**: OpenClaw is from the same "lobster" family as Hermes/Pi, so the one-shot CLI + custom-provider-config pattern transfers directly. The reviewer role is the safest experiment surface for a fresh backend: per the project's reviewer-design rule, the reviewer performer only provides feedback and **humans approve/merge**, so a flaky new backend can never merge anything; its binary JSON-only contract also isolates backend behavior cleanly.
- **POC (2026-05-29 — proven end-to-end against live LiteLLM + spark/qwen3.6:35b):**
  - Install: `npm install -g openclaw` into `coordinare-performer:full` (Node 22 present). **OpenClaw 2026.5.27**. → entrypoint install method confirmed.
  - Config path is **`~/.openclaw/openclaw.json`** (HOME-based; legacy `~/.clawdbot/clawdbot.json` is auto-symlinked). Schema:
    `{"models":{"mode":"merge","providers":{"litellm":{"baseUrl":"<proxy>/v1","apiKey":"${LITELLM_MASTER_KEY}","api":"openai-completions","timeoutSeconds":300,"models":[{"id":"spark/qwen3.6:35b",...}]}}},"agents":{"defaults":{"models":{"litellm/spark/qwen3.6:35b":{"alias":"..."}}}}}`.
  - **Model allowlist is REQUIRED** — OpenClaw rejects any model not listed under `agents.defaults.models`. `mode:"merge"` preserves bundled provider defaults (a partial write without it wipes the providers object).
  - **No manual `compat` block needed** (unlike Pi) — OpenClaw auto-forces `compat.supportsDeveloperRole:false` for non-native `openai-completions` endpoints, so qwen accepts it directly.
  - Run: `openclaw agent --local --json --session-key <key> --model litellm/spark/qwen3.6:35b --message "<task>"`. `--local` runs the embedded agent (no Gateway); a session selector (`--session-key`, any string) is required even with `--local`; **no agent definition needed**.
  - JSON output (top-level `{payloads, meta}`): `payloads[0].text` = delivered reply; `meta.finalAssistantVisibleText`/`finalAssistantRawText` = final text; `meta.stopReason` ("stop" = success); `meta.aborted`; `meta.executionTrace.{winnerProvider,winnerModel}` confirmed `litellm` / `spark/qwen3.6:35b`. **No token-usage field.** Returned `pong`.
  - `OpenClawBackend` parser: rc 0 + `stopReason=="stop"` + not `aborted` → done (output = `payloads[0].text`); else error. Reuses `opencode._build_task_prompt` (persona-in-body + role JSON-only contract).
- **Alternatives considered**: subclass HermesBackend — rejected (Hermes routes persona to `SOUL.md` and uses `hermes chat`-specific flags/JSON; OpenClaw's CLI surface and `{payloads,meta}` JSON differ enough that a focused adapter is clearer than overriding most of Hermes).
