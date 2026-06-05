# Persona benchmark — `qwen36-35b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ✅ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b |
| architect | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b |
| implementer | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b |
| reviewer | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ❌ qwen3.6:35b |
| security | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | 🔧 qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b |
| qa | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | 🔧 qwen3.6:35b | ❌ qwen3.6:35b | ❌ qwen3.6:35b | ✅ qwen3.6:35b |
| tech_writer | ✅ qwen3.6:35b | ✅ qwen3.6:35b | 💥 qwen3.6:35b | ❌ qwen3.6:35b | ✅ qwen3.6:35b | ❌ qwen3.6:35b | ✅ qwen3.6:35b |
| closer | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b |
| env_bootstrap | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b | ✅ qwen3.6:35b |

## Per-persona — which (backend, model) PASSed

- **assessor**: claude(qwen3.6:35b)
- **architect**: claude(qwen3.6:35b), codex(qwen3.6:35b), hermes(qwen3.6:35b), junie(qwen3.6:35b), openclaw(qwen3.6:35b), opencode(qwen3.6:35b), pi(qwen3.6:35b)
- **implementer**: claude(qwen3.6:35b), codex(qwen3.6:35b), hermes(qwen3.6:35b), junie(qwen3.6:35b), openclaw(qwen3.6:35b), opencode(qwen3.6:35b), pi(qwen3.6:35b)
- **reviewer**: claude(qwen3.6:35b), codex(qwen3.6:35b), hermes(qwen3.6:35b), junie(qwen3.6:35b), openclaw(qwen3.6:35b), opencode(qwen3.6:35b)
- **security**: — none passed —
- **qa**: pi(qwen3.6:35b)
- **tech_writer**: claude(qwen3.6:35b), codex(qwen3.6:35b), openclaw(qwen3.6:35b), pi(qwen3.6:35b)
- **closer**: claude(qwen3.6:35b), codex(qwen3.6:35b), hermes(qwen3.6:35b), junie(qwen3.6:35b), openclaw(qwen3.6:35b), opencode(qwen3.6:35b), pi(qwen3.6:35b)
- **env_bootstrap**: claude(qwen3.6:35b), codex(qwen3.6:35b), hermes(qwen3.6:35b), junie(qwen3.6:35b), openclaw(qwen3.6:35b), opencode(qwen3.6:35b), pi(qwen3.6:35b)

## Totals

ERROR=1, FAIL_HARNESS=2, FAIL_MODEL=20, PASS=40
