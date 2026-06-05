# Persona benchmark — `qwen25coder-latest`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest |
| architect | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest |
| implementer | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | 💥 qwen2.5-coder:latest |
| reviewer | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest |
| security | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest |
| qa | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest |
| tech_writer | ✅ qwen2.5-coder:latest | ❌ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest |
| closer | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest |
| env_bootstrap | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | 💥 qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest | ✅ qwen2.5-coder:latest |

## Per-persona — which (backend, model) PASSed

- **assessor**: opencode(qwen2.5-coder:latest)
- **architect**: claude(qwen2.5-coder:latest), codex(qwen2.5-coder:latest), hermes(qwen2.5-coder:latest), openclaw(qwen2.5-coder:latest), opencode(qwen2.5-coder:latest), pi(qwen2.5-coder:latest)
- **implementer**: — none passed —
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(qwen2.5-coder:latest), hermes(qwen2.5-coder:latest), openclaw(qwen2.5-coder:latest), opencode(qwen2.5-coder:latest), pi(qwen2.5-coder:latest)
- **closer**: claude(qwen2.5-coder:latest), codex(qwen2.5-coder:latest), hermes(qwen2.5-coder:latest), openclaw(qwen2.5-coder:latest), opencode(qwen2.5-coder:latest), pi(qwen2.5-coder:latest)
- **env_bootstrap**: claude(qwen2.5-coder:latest), codex(qwen2.5-coder:latest), hermes(qwen2.5-coder:latest), openclaw(qwen2.5-coder:latest), opencode(qwen2.5-coder:latest), pi(qwen2.5-coder:latest)

## Totals

ERROR=15, FAIL_MODEL=24, PASS=24
