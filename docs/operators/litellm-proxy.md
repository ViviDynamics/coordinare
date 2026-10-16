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
   `LITELLM_PROXY_AUTH_TOKEN`. A base URL that already ends in `/v1`
   (e.g. `http://litellm:4000/v1`) is tolerated: the shim joins the CLI's
   request path to a single `/v1` instead of doubling it (`/v1/v1/...`
   404s at the upstream). The bare origin (`http://litellm:4000`) remains
   the documented form.
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
| `local/qwen3.6:35b` | the model host (via `litellm.example`) | 2026-05-24 | SC-006 smoke gate, 5 sequential `claude --print` calls, zero parser errors |

## SC-006 gate: smoke test

`scripts/smoke_claude_via_litellm.sh` is the spec-073 acceptance gate
for SC-006 (no `Content block not found` parser errors when routing a
non-Anthropic model through LiteLLM). The script:

1. Reads `LITELLM_MASTER_KEY` from `.env` at repo root.
2. Spawns the in-process `ClaudeCodeShim` against the configured
   upstream (defaults to `https://litellm.example`) and
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
scripts/smoke_claude_via_litellm.sh                       # default: local/qwen3.6:35b
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

## GLM through Chat Completions (262)

For `glm-5.3-flash`, use the active `claude_code` entry in
`routing.example.yaml`: `strategy: translate`, OpenAI wire, both
`strip_control_chars` and `strip_reasoning`, and a completion probe. The CLI
and routing key both use `glm-5.3-flash` in this example. Route selection is
an exact `(backend, model)` string match: keep the CLI `--model` value and
the routing entry’s `model` identical, including any namespace prefix. Set
`upstream_model` to the identifier the upstream OpenAI endpoint expects; it
may differ from the routing key. The startup probe judges the normalized completion, so a
reasoning-only reply is usable. Tool calls remain structured through translation.

Mount the routing table and explicitly supply its authentication variable on
`claude-ephemeral`; a host-only variable is insufficient inside the container:

```yaml
performer_endpoints:
  - id: claude-ephemeral
    mode: ephemeral
    image: coordinare-performer:extra
    roles: [assessor, qa, tech_writer, env_bootstrap]
    env:
      BACKEND: claude_code
      SELFHOSTED_ROUTING_CONFIG: /etc/coordinare/routing.yaml
      SELFHOSTED_HEALTH_TIMEOUT: "120"
      COORDINARE_PROXY_AUTH: "${LITELLM_MASTER_KEY}"
      LITELLM_PROXY_AUTH_TOKEN: "${LITELLM_MASTER_KEY}"
      LITELLM_PROXY_BASE_URL: https://litellm.example.com
    volumes:
      - host_path: /absolute/path/to/routing.yaml
        container_path: /etc/coordinare/routing.yaml
        mode: ro
```

Select `backend: claude_code` with the GLM single-model mode on the relevant
roles, and set the symphony's `env_bootstrap_performer_id: claude-ephemeral`
when using it for bootstrap. Validate configuration before restart. This is
an operator opt-in; the generic Anthropic-native example remains available.
Do not apply `disable_thinking` to this GLM model: the recorded policy experiment
was harmful. The translate route repairs the wire adaptation instead.

## Assessor on a reasoning model behind LiteLLM (223)

Symptom: the assessor's JSON reply starts mid-token (`sufficient": true, ...`)
with a reasoning model such as glm-5.3-flash behind LiteLLM `/v1/messages`.
LiteLLM v1.103.2's Anthropic adapter drops a `content` delta that shares a
chunk with `reasoning`. The issue's reproduction found `/v1/chat/completions`
intact in 2 of 2 runs, which is a small sample. Upstream BerriAI/litellm#27492
reports that `use_chat_completions_api: true` can also drop content when the
provider returns `reasoning_content`, so that path is not proven safe either.
The performer's translate route avoids the adapter: it turns the CLI's
`/v1/messages` calls into `/v1/chat/completions` itself, reads `delta.content`
independently of reasoning. `strip_reasoning` removes both `reasoning` and
`reasoning_content`. Only a legacy `reasoning_content` reply is promoted to text,
and never one that ended on `length`. A reasoning-only `reasoning` reply stays
empty, so it fails loudly rather than handing the parser a draft.

Set the routing entry's `model` to the exact model string the assessor
dispatches, including any namespace prefix your gateway adds to the model name.
`upstream_model` is the name the LiteLLM gateway advertises for the model:

`routing.example.yaml` includes a `gateway/glm-5.3-flash` entry illustrating a
namespaced model. Replace its model name and gateway address before using it.

```yaml
selfhosted_routing:
  - backend: claude_code
    model: <dispatched-model-name>          # exact dispatched model string (routing key)
    target:
      base_url: https://litellm.example.com/v1
      wire_format: openai
      strategy: translate
      upstream_model: <gateway-model-name>  # name the gateway advertises
      upstream_auth_env: ANTHROPIC_AUTH_TOKEN # gateway bearer from catalog endpoint
      normalizers: [strip_control_chars, strip_reasoning]
      health_probe: completion
```

The activation key is the existing `SELFHOSTED_ROUTING_CONFIG` performer env
setting, which defaults to empty. No additional switch is needed. A matching
translate target replaces the ordinary spec-080 proxy path and suppresses the
older `ClaudeCodeShim`, so there is only one local shim. A missing entry keeps
the current path. Text blocks open only on non-empty content; reasoning-only
deltas do not create an empty leading block. Use a single-model mode without a
reasoning policy for this route. Keep `disable_thinking` off for this model.

For garden Kubernetes performers (557), deploy a daemon image containing this
endpoint setting, then follow these steps. No derived performer image is needed.

1. Keep the assessor on `backend: claude_code` with its existing GLM mode.
   The mode's catalog endpoint must supply the LiteLLM base URL and
   `auth_env` naming the daemon's gateway credential. Coordinare injects this
   into the job as `ANTHROPIC_AUTH_TOKEN`; the route above reuses it for both
   the startup probe and actual traffic. Do not put a credential in routing YAML.
2. Save the routing YAML above as `routing.yaml`, replacing the routing key and,
   if needed, `upstream_model` with the actual dispatched and gateway names.
   Create a ConfigMap in the **performer namespace**, the daemon config's
   `kubernetes_namespace` (Helm `performers.namespace`, default release namespace).
   These commands assume namespace and release `coordinare`; change `PNS` if
   performers run elsewhere:

   ```sh
   NS=coordinare
   PNS=coordinare
   kubectl -n "$PNS" create configmap coordinare-performer-routing \
     --from-file=routing.yaml=./routing.yaml --dry-run=client -o yaml | kubectl apply -f -
   ```

3. In the garden `coordinare` ConfigMap's `config.yaml`, add this block to the
   **existing** endpoint whose `roles` includes `assessor`. Preserve its image,
   other roles, env and cache settings. Remove any stale routing host-path mount
   and `env.SELFHOSTED_ROUTING_CONFIG` from that Kubernetes endpoint:

   ```yaml
   performer_endpoints:
     - id: claude-ephemeral  # use the existing endpoint ID
       mode: ephemeral
       image: coordinare-performer:full  # retain the existing image pin
       roles: [assessor]  # retain all existing roles
       kubernetes_routing_configmap:
         name: coordinare-performer-routing
         key: routing.yaml  # optional; defaults to routing.yaml
   ```

   ```sh
   kubectl -n "$NS" edit configmap coordinare
   ```

   With Helm, keep the same setting under `config.performer_endpoints` in your
   values source. If `configPersistence.enabled` is true (the default), also
   update the existing endpoint in the **live**
   `/var/lib/coordinare/config/config.yaml` via your normal config-editing
   workflow. Editing only the seed ConfigMap or Helm values does not replace
   previously seeded user endpoint settings. See
   [persistent configuration](../../deploy/helm/coordinare/README.md#persistent-configuration-and-upgrades).
   Do not reseed the whole file just to change this endpoint.

4. Validate the active daemon config with its existing secret environment, then
   restart the daemon after current work drains and dispatch a fresh assessor.
   For Helm's default persistent path and StatefulSet name:

   ```sh
   kubectl -n "$NS" exec coordinare-0 -c coordinare -- \
     python -m coordinare config validate --config /var/lib/coordinare/config/config.yaml
   kubectl -n "$NS" rollout restart statefulset/coordinare
   kubectl -n "$NS" rollout status statefulset/coordinare
   ```

   For ConfigMap-only deployments, validate `/etc/coordinare/config.yaml`
   instead, after the edited ConfigMap has propagated. The runtime mounts only
   the selected key read-only at `/etc/coordinare/routing/routing.yaml` and sets
   `SELFHOSTED_ROUTING_CONFIG` to that path, overriding any explicit endpoint env
   value. Omitted/null means no mount or env change. Docker retains its existing
   host-path + env configuration.

The ConfigMap must exist in the performer namespace, with the chosen key;
otherwise Kubernetes keeps the pod waiting and reports a mount error in pod
events. The daemon needs no additional RBAC: the kubelet resolves the pod's
ConfigMap reference, and performers still have no service-account token. The
daemon never reads the routing file. Existing jobs do not reload routing; use
fresh jobs after changing it. Confirm `selfhosted_layer.translate_launched`
and upstream `/v1/chat/completions` requests.

A derived performer image remains an alternative: include
`COPY routing.yaml /etc/coordinare/routing.yaml`, set the endpoint image to its
immutable tag, omit `kubernetes_routing_configmap`, and explicitly set
`env.SELFHOSTED_ROUTING_CONFIG: /etc/coordinare/routing.yaml`. Mounting the file
only in the daemon pod is insufficient.

The daemon's `config validate` does not validate a separately mounted routing
file. Inside the performer image, validate that file with:

```sh
python -c 'from performer.proxy.routing import RoutingTable; RoutingTable.from_yaml_file("/etc/coordinare/routing/routing.yaml")'
```

For example, run that command with `kubectl -n "$PNS" exec <new-performer-pod>
-- python -c ...` while the fresh pod is alive. Use `/etc/coordinare/routing.yaml`
for the Docker or derived-image examples.

Re-check the upstream LiteLLM release notes before removing this route: the adapter bug was
still open upstream as of 2026-10-15 (see issues 32357 and 27492 and PR 32364
on BerriAI/litellm).
