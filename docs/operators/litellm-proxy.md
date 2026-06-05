# LiteLLM Proxy for the `claude_code` Backend (Spec 073)

**Audience**: Operator who wants to run the `claude_code` performer backend
with LLM traffic routed through a LiteLLM proxy (their own, or one they're
about to stand up).

**Time to first dispatched card**: under 30 minutes (SC-005).

## How it works

The performer process reads two settings from its environment:

| Setting (env var) | Forwarded to claude CLI as |
|-------------------|----------------------------|
| `LITELLM_PROXY_BASE_URL` | `ANTHROPIC_BASE_URL` |
| `LITELLM_PROXY_AUTH_TOKEN` | `ANTHROPIC_AUTH_TOKEN` |

When `LITELLM_PROXY_BASE_URL` is **non-empty** AND the active backend is
`claude_code`, the performer injects both values into the `claude` CLI
subprocess env. The CLI itself does the rest — it speaks the Anthropic
Messages protocol against whatever endpoint `ANTHROPIC_BASE_URL` points at.

When `LITELLM_PROXY_BASE_URL` is empty (or unset), the subprocess env is
byte-identical to the pre-feature baseline; the CLI uses its default
endpoint (`https://api.anthropic.com`).

These vars are **only** injected for the `claude_code` backend
(FR-005). The opencode, junie, codex, and hermes backends do not
read them.

### The in-container response shim

The `claude` CLI's response parser rejects `thinking` content blocks
with `Content block not found` when LiteLLM forwards them from a
non-Anthropic upstream. To stay compatible, the performer spawns a tiny
in-process reverse proxy (`claude_code_shim.py`) when
`LITELLM_PROXY_BASE_URL` is set:

1. The shim binds `127.0.0.1:<ephemeral>` and is the actual value the
   CLI sees in `ANTHROPIC_BASE_URL` — the operator's real URL stays
   only inside the shim.
2. It forwards `POST /v1/messages` (streaming and non-streaming) to the
   upstream LiteLLM URL, attaching the bearer from
   `LITELLM_PROXY_AUTH_TOKEN`.
3. It strips `thinking` content blocks from JSON responses and SSE
   event streams, renumbering remaining block indices.
4. Non-`/v1/messages` paths pass through unmodified.

If the shim fails to start, the performer surfaces an error status and
does **not** silently fall back to direct-Anthropic (FR-010). The shim
logs one INFO line per request — method, path, status, latency — and
never logs request bodies or auth headers (FR-011).

## Prerequisites

- A working coordinare + performer deployment with `AGENT_BACKEND=claude_code`.
- Either an Anthropic API key (validate against Anthropic via the proxy) or a
  non-Anthropic provider key matching a row in the Validated Model Matrix below.

## Option A — You already have a LiteLLM proxy

1. Set on the performer process:
   ```bash
   export LITELLM_PROXY_BASE_URL="https://your-litellm.example.com"
   export LITELLM_PROXY_AUTH_TOKEN="sk-litellm-..."
   ```
2. Restart the performer.
3. Dispatch one card. Confirm the proxy logs show the inbound request and
   the card reaches `submitted`.

## Option B — Bring up a minimal LiteLLM proxy locally

1. Create `litellm-config.yaml`:
   ```yaml
   model_list:
     - model_name: claude-opus-4-7
       litellm_params:
         model: anthropic/claude-opus-4-20250514
         api_key: os.environ/ANTHROPIC_API_KEY
   ```
2. Run the proxy:
   ```bash
   docker run --rm -p 4000:4000 \
     -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
     -v "$PWD/litellm-config.yaml:/app/config.yaml" \
     ghcr.io/berriai/litellm:main-latest \
     --config /app/config.yaml --port 4000
   ```
3. On the performer:
   ```bash
   export LITELLM_PROXY_BASE_URL="http://localhost:4000"
   export LITELLM_PROXY_AUTH_TOKEN="sk-1234"   # LiteLLM's default master key
   ```
4. Restart the performer; dispatch one card; verify proxy logs and card completion.

## Routing a non-Anthropic model

1. Add a route to `litellm-config.yaml`:
   ```yaml
     - model_name: claude-opus-4-7      # name the CLI sends
       litellm_params:
         model: openai/gpt-4o           # what LiteLLM routes to
         api_key: os.environ/OPENAI_API_KEY
   ```
2. Re-run the proxy.
3. Check the Validated Model Matrix below for confirmed-working pairs before
   betting on a specific downstream provider for production cards.

## Validated Model Matrix

| Model name (CLI) | Downstream provider | Date validated | Card scenario |
|------------------|---------------------|----------------|---------------|
| `claude-opus-4-20250514` | `anthropic/claude-opus-4-20250514` | _pending live validation_ | full-cycle dispatch |
| `spark/qwen3.6:35b` | Spark (via `litellm.vividynamics.com`) | 2026-05-24 | SC-006 smoke gate, 5 sequential `claude --print` calls, zero parser errors |

## SC-006 gate: smoke test

`scripts/smoke_claude_via_litellm.sh` is the spec-073 acceptance gate
for SC-006 (no `Content block not found` parser errors when routing a
non-Anthropic model through LiteLLM). The script:

1. Reads `LITELLM_MASTER_KEY` from `.env` at repo root.
2. Spawns the in-process `ClaudeCodeShim` against the configured
   upstream (defaults to `https://litellm.vividynamics.com`) and
   captures its ephemeral loopback port.
3. Exports `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` and
   `ANTHROPIC_AUTH_TOKEN=<master key>` (and unsets
   `ANTHROPIC_API_KEY`).
4. Runs 5 sequential `claude --print --model <model>` invocations,
   appending each stderr to a single capture file.
5. Greps the stderr for `Content block not found`. Any hit fails the
   gate (exit 1). Any non-zero CLI exit across the 5 calls also fails.

Run:

```sh
scripts/smoke_claude_via_litellm.sh                       # default: spark/qwen3.6:35b
scripts/smoke_claude_via_litellm.sh openrouter/qwen-3.5-72b
```

Every model added to the Validated Model Matrix above must pass this
gate on the date listed.

## Troubleshooting

- **Card fails immediately with "401"**: `LITELLM_PROXY_AUTH_TOKEN` mismatch
  with the proxy's master key. Compare values; restart performer.
- **Card hangs**: Proxy reachable but slow downstream. The claude CLI's own
  timeout governs; the performer adds no extra. Check proxy logs.
- **Wanted Anthropic-direct but traffic still hits proxy**: Unset
  `LITELLM_PROXY_BASE_URL` (or set it to empty) and restart the performer —
  when unset, no env var is injected and the CLI uses its built-in endpoint.

## Security

- `LITELLM_PROXY_AUTH_TOKEN` is a secret — treat it the same way you treat
  `ANTHROPIC_API_KEY`. The performer never logs it (FR-004 / SC-004); a
  regex denylist in `BackendEvent` strips `sk-litellm-…` tokens out of any
  event text that does flow through structured logging.
- The performer does not persist either value to card state.
