# Persona benchmark — `qwen3-8b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | 💥 qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | ✅ qwen3:8b |
| architect | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b |
| implementer | 💥 qwen3:8b | ✅ qwen3:8b | 💥 qwen3:8b | ✅ qwen3:8b | 💥 qwen3:8b | 💥 qwen3:8b | ❌ qwen3:8b |
| reviewer | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | ✅ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b |
| security | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | 🔧 qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b |
| qa | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | 🔧 qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b | ❌ qwen3:8b |
| tech_writer | ✅ qwen3:8b | ✅ qwen3:8b | 💥 qwen3:8b | 🔧 qwen3:8b | 🔧 qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b |
| closer | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b |
| env_bootstrap | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b | ✅ qwen3:8b |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(qwen3:8b)
- **architect**: claude(qwen3:8b), codex(qwen3:8b), hermes(qwen3:8b), junie(qwen3:8b), openclaw(qwen3:8b), opencode(qwen3:8b), pi(qwen3:8b)
- **implementer**: codex(qwen3:8b), junie(qwen3:8b)
- **reviewer**: junie(qwen3:8b)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(qwen3:8b), codex(qwen3:8b), opencode(qwen3:8b), pi(qwen3:8b)
- **closer**: claude(qwen3:8b), codex(qwen3:8b), hermes(qwen3:8b), junie(qwen3:8b), openclaw(qwen3:8b), opencode(qwen3:8b), pi(qwen3:8b)
- **env_bootstrap**: claude(qwen3:8b), codex(qwen3:8b), hermes(qwen3:8b), junie(qwen3:8b), openclaw(qwen3:8b), opencode(qwen3:8b), pi(qwen3:8b)

## Totals

ERROR=6, FAIL_HARNESS=4, FAIL_MODEL=24, PASS=29
