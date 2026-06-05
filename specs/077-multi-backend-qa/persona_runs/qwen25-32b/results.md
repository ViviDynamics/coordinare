# Persona benchmark — `qwen25-32b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b |
| architect | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b |
| implementer | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | 💥 qwen2.5:32b |
| reviewer | ❌ qwen2.5:32b | ❌ qwen2.5:32b | 💥 qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ✅ qwen2.5:32b |
| security | ❌ qwen2.5:32b | 🔧 qwen2.5:32b | ❌ qwen2.5:32b | 💥 qwen2.5:32b | 🔧 qwen2.5:32b | 🔧 qwen2.5:32b | ❌ qwen2.5:32b |
| qa | ❌ qwen2.5:32b | 🔧 qwen2.5:32b | 💥 qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b | ❌ qwen2.5:32b |
| tech_writer | ✅ qwen2.5:32b | ✅ qwen2.5:32b | 💥 qwen2.5:32b | 💥 qwen2.5:32b | 🔧 qwen2.5:32b | ❌ qwen2.5:32b | ✅ qwen2.5:32b |
| closer | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b |
| env_bootstrap | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b | ✅ qwen2.5:32b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(qwen2.5:32b), codex(qwen2.5:32b), hermes(qwen2.5:32b), junie(qwen2.5:32b), openclaw(qwen2.5:32b), opencode(qwen2.5:32b), pi(qwen2.5:32b)
- **implementer**: claude(qwen2.5:32b), codex(qwen2.5:32b), hermes(qwen2.5:32b), junie(qwen2.5:32b), openclaw(qwen2.5:32b), opencode(qwen2.5:32b)
- **reviewer**: pi(qwen2.5:32b)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(qwen2.5:32b), codex(qwen2.5:32b), pi(qwen2.5:32b)
- **closer**: claude(qwen2.5:32b), codex(qwen2.5:32b), hermes(qwen2.5:32b), junie(qwen2.5:32b), openclaw(qwen2.5:32b), opencode(qwen2.5:32b), pi(qwen2.5:32b)
- **env_bootstrap**: claude(qwen2.5:32b), codex(qwen2.5:32b), hermes(qwen2.5:32b), junie(qwen2.5:32b), openclaw(qwen2.5:32b), opencode(qwen2.5:32b), pi(qwen2.5:32b)

## Totals

ERROR=6, FAIL_HARNESS=5, FAIL_MODEL=21, PASS=31
