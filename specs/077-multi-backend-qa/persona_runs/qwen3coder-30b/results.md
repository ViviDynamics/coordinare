# Persona benchmark — `qwen3coder-30b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ✅ qwen3-coder:30b |
| architect | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b |
| implementer | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b |
| reviewer | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | 💥 qwen3-coder:30b | ✅ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b |
| security | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | 🔧 qwen3-coder:30b | ❌ qwen3-coder:30b | 🔧 qwen3-coder:30b | ❌ qwen3-coder:30b |
| qa | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | 🔧 qwen3-coder:30b | 🔧 qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b | ❌ qwen3-coder:30b |
| tech_writer | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | 🔧 qwen3-coder:30b | 🔧 qwen3-coder:30b | 🔧 qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b |
| closer | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b |
| env_bootstrap | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b | ✅ qwen3-coder:30b |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(qwen3-coder:30b)
- **architect**: claude(qwen3-coder:30b), codex(qwen3-coder:30b), hermes(qwen3-coder:30b), junie(qwen3-coder:30b), openclaw(qwen3-coder:30b), opencode(qwen3-coder:30b), pi(qwen3-coder:30b)
- **implementer**: claude(qwen3-coder:30b), codex(qwen3-coder:30b), hermes(qwen3-coder:30b), junie(qwen3-coder:30b), openclaw(qwen3-coder:30b), opencode(qwen3-coder:30b), pi(qwen3-coder:30b)
- **reviewer**: junie(qwen3-coder:30b)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(qwen3-coder:30b), codex(qwen3-coder:30b), opencode(qwen3-coder:30b), pi(qwen3-coder:30b)
- **closer**: claude(qwen3-coder:30b), codex(qwen3-coder:30b), hermes(qwen3-coder:30b), junie(qwen3-coder:30b), openclaw(qwen3-coder:30b), opencode(qwen3-coder:30b), pi(qwen3-coder:30b)
- **env_bootstrap**: claude(qwen3-coder:30b), codex(qwen3-coder:30b), hermes(qwen3-coder:30b), junie(qwen3-coder:30b), openclaw(qwen3-coder:30b), opencode(qwen3-coder:30b), pi(qwen3-coder:30b)

## Totals

ERROR=1, FAIL_HARNESS=7, FAIL_MODEL=21, PASS=34
