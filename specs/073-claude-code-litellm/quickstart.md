# Quickstart: Route claude_code through a LiteLLM proxy

**Audience**: Operator who wants to run the `claude_code` performer backend with LLM traffic routed via a LiteLLM proxy (their own, or one they're about to stand up).

**Time**: Under 30 minutes (SC-005).

## Prerequisites

- A working coordinare + performer deployment with `AGENT_BACKEND=claude_code`.
- Docker (for the local LiteLLM proxy bring-up, optional).
- Either an Anthropic API key (to validate against Anthropic via the proxy) or a non-Anthropic provider key matching one of the rows in the Validated Model Matrix.

## Option A — You already have a LiteLLM proxy

1. Set on the performer process:
   ```bash
   export LITELLM_PROXY_BASE_URL="https://your-litellm.example.com"
   export LITELLM_PROXY_AUTH_TOKEN="sk-litellm-..."
   ```
2. Restart the performer.
3. Dispatch one card. Confirm the proxy logs show the inbound request and the card reaches `submitted`.

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
   export LITELLM_PROXY_AUTH_TOKEN="sk-1234"   # any token; LiteLLM's default master key
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
3. Check the Validated Model Matrix below for confirmed-working pairs before betting on a specific downstream provider for production cards.

## Validated Model Matrix

| Model name (CLI) | Downstream provider | Date validated | Card scenario |
|------------------|--------------------|---------------|---------------|
| _to be populated during /speckit.tasks_ | | | |

## SC-006 gate: smoke test

`scripts/smoke_claude_via_litellm.sh` is the spec-073 acceptance gate for
SC-006 (no `Content block not found` parser errors when routing a
non-Anthropic model through LiteLLM). The script:

1. Reads `LITELLM_MASTER_KEY` from `.env` at repo root.
2. Spawns the in-process `ClaudeCodeShim` against the configured upstream
   (`https://litellm.vividynamics.com` by default) and captures its
   ephemeral loopback port.
3. Exports `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` and
   `ANTHROPIC_AUTH_TOKEN=<master key>` (and unsets `ANTHROPIC_API_KEY`).
4. Runs 5 sequential `claude --print --model <model>` invocations, appending
   each stderr to a single capture file.
5. Greps the stderr for `Content block not found`. **Any hit fails the gate**
   (exit 1). Any non-zero CLI exit across the 5 calls also fails.

Run:

```sh
scripts/smoke_claude_via_litellm.sh                       # default: spark/qwen3.6:35b
scripts/smoke_claude_via_litellm.sh openrouter/qwen-3.5-72b
```

Every model added to the Validated Model Matrix above must pass this gate on
the date listed.

## Troubleshooting

- **Card fails immediately with "401"**: `LITELLM_PROXY_AUTH_TOKEN` mismatch with the proxy's master key. Compare values; restart performer.
- **Card hangs**: Proxy reachable but slow downstream. The claude CLI's own timeout governs; the performer adds no extra. Check proxy logs.
- **Wanted Anthropic-direct but traffic still hits proxy**: Unset `LITELLM_PROXY_BASE_URL` (or set it to empty) and restart the performer — when unset, no env var is injected and the CLI uses its built-in endpoint.

## Security

- `LITELLM_PROXY_AUTH_TOKEN` is a secret — treat it the same way you treat `ANTHROPIC_API_KEY`. It is never logged by the performer.
- The performer does not persist either value to card state.
