# Persona benchmark — `gemma3-27b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b |
| architect | ✅ gemma3:27b | ✅ gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 💥 gemma3:27b |
| implementer | ✅ gemma3:27b | ✅ gemma3:27b | 🔧 gemma3:27b | ✅ gemma3:27b | 🔧 gemma3:27b | ✅ gemma3:27b | ✅ gemma3:27b |
| reviewer | ❌ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b |
| security | ❌ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b |
| qa | ❌ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b |
| tech_writer | ✅ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | ✅ gemma3:27b |
| closer | ✅ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b |
| env_bootstrap | ✅ gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | 🔧 gemma3:27b | 💥 gemma3:27b | ✅ gemma3:27b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(gemma3:27b), codex(gemma3:27b)
- **implementer**: claude(gemma3:27b), codex(gemma3:27b), junie(gemma3:27b), opencode(gemma3:27b), pi(gemma3:27b)
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(gemma3:27b), pi(gemma3:27b)
- **closer**: claude(gemma3:27b)
- **env_bootstrap**: claude(gemma3:27b), pi(gemma3:27b)

## Totals

ERROR=24, FAIL_HARNESS=23, FAIL_MODEL=4, PASS=12
