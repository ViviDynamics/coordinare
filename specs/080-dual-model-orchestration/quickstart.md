# Quickstart: Dual-Model Orchestration (080)

How an operator configures and exercises the feature. Assumes the 080 branch is built and the performer image rebuilt.

## 1. Declare the catalogs (config.yaml root)

```yaml
endpoints:
  - name: spark-litellm
    kind: litellm
    base_url: http://spark:4000
    auth_env: LITELLM_PROXY_AUTH_TOKEN
  - name: spark-ollama
    kind: ollama
    base_url: http://spark:11434
  - name: anthropic-cloud
    kind: anthropic
    auth_env: ANTHROPIC_API_KEY

model_endpoints:
  - { name: gptoss120b-litellm, endpoint: spark-litellm, model: spark/gpt-oss:120b }
  - { name: gptoss120b-ollama,  endpoint: spark-ollama,  model: gpt-oss:120b }
  - { name: qwen36-litellm,     endpoint: spark-litellm, model: spark/qwen3.6:35b }
  - { name: claude-sonnet,      endpoint: anthropic-cloud, model: claude-sonnet-4-5 }

modes:
  - { name: native-claude-sonnet, strategy: single, tool: claude-sonnet }
  - name: always-gptoss120b
    strategy: always
    thinking: gptoss120b-litellm
    tool: gptoss120b-ollama
    expose_plan_as: thinking
  - name: conditional-gptoss-qwen
    strategy: conditional
    thinking: gptoss120b-litellm
    tool: qwen36-litellm
    classifier: qwen36-litellm
    threshold: 0.6
```

## 2. Point performers at modes

```yaml
performers:
  reviewer:   { backend: claude_code, mode: native-claude-sonnet }   # single, native, no proxy
  architect:  { backend: codex,       mode: always-gptoss120b }      # dual-model
  implementer:{ backend: codex,       mode: conditional-gptoss-qwen } # dual-model, escalation
```

## 3. Migration (hard cut)

- Remove every inline `performers.<role>.model:` — config load now **errors** if present, pointing you to the catalog.
- Each previously-single-model role becomes a `strategy: single` mode referencing one `model_endpoint`.
- Migrate the in-repo `config.example.*.yaml` and your gitignored operator `config*.yaml`.
- **hermes** can only use `strategy: single` (no provider-base-URL override yet).
- **Self-hosted single-mode auth:** the endpoint `auth_env` reaches `claude_code` (bearer), `codex`, `opencode`, and `junie` via the dispatch path. `pi`/`openclaw` single-mode self-hosted auth still flows through their container/endpoint provider env (`PI_PROVIDER_*` / `OPENCLAW_PROVIDER_*`), not the catalog `auth_env`. The multi-model proxy path honors `auth_env` for all backends.

## 4. Validate

```bash
.venv/bin/python -m coordinare --config config.yaml --validate   # or the CLI validate command
```
Expect actionable errors for: dangling refs, leftover inline `model:`, native endpoint with `base_url`, `single` mode with `thinking:`, `conditional` missing `classifier`/`threshold`.

## 5. Run & observe

- `single` modes: identical to today, no proxy, zero added latency.
- `always`/`conditional`/`think_once`: the in-container `DualModelProxy` launches; the agent CLI is transparently pointed at it.
- Inspect the job capture dir for the per-turn `OrchestrationRecord` (strategy decision, classifier score, plan text, per-call latencies) — this is how you measure per-persona value.

## 6. Acceptance smoke

- Run one card stage on `always-gptoss120b`; confirm in the capture artifacts that the thinking model produced the plan and the tool model produced the executed `tool_calls` (SC-002).
- Run a `single` role; confirm no proxy process and no latency delta (SC-003).
