# Persona benchmark — `deepseek-r1-32b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b |
| architect | ✅ deepseek-r1:32b | ✅ deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 💥 deepseek-r1:32b |
| implementer | ✅ deepseek-r1:32b | ✅ deepseek-r1:32b | 🔧 deepseek-r1:32b | ✅ deepseek-r1:32b | 🔧 deepseek-r1:32b | ✅ deepseek-r1:32b | ✅ deepseek-r1:32b |
| reviewer | ❌ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b |
| security | ❌ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b |
| qa | ❌ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b |
| tech_writer | ✅ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | ✅ deepseek-r1:32b |
| closer | ✅ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b |
| env_bootstrap | ✅ deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | 🔧 deepseek-r1:32b | 💥 deepseek-r1:32b | ✅ deepseek-r1:32b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(deepseek-r1:32b), codex(deepseek-r1:32b)
- **implementer**: claude(deepseek-r1:32b), codex(deepseek-r1:32b), junie(deepseek-r1:32b), opencode(deepseek-r1:32b), pi(deepseek-r1:32b)
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(deepseek-r1:32b), pi(deepseek-r1:32b)
- **closer**: claude(deepseek-r1:32b)
- **env_bootstrap**: claude(deepseek-r1:32b), pi(deepseek-r1:32b)

## Totals

ERROR=24, FAIL_HARNESS=23, FAIL_MODEL=4, PASS=12
