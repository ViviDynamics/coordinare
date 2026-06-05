# Persona benchmark — `deepseek-r1-70b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b |
| architect | ✅ deepseek-r1:70b | ✅ deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 💥 deepseek-r1:70b |
| implementer | ✅ deepseek-r1:70b | ✅ deepseek-r1:70b | 🔧 deepseek-r1:70b | ✅ deepseek-r1:70b | 🔧 deepseek-r1:70b | ✅ deepseek-r1:70b | ✅ deepseek-r1:70b |
| reviewer | ❌ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b |
| security | ❌ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b |
| qa | ❌ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b |
| tech_writer | ✅ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | ✅ deepseek-r1:70b |
| closer | ✅ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b |
| env_bootstrap | ✅ deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | 🔧 deepseek-r1:70b | 💥 deepseek-r1:70b | ✅ deepseek-r1:70b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(deepseek-r1:70b), codex(deepseek-r1:70b)
- **implementer**: claude(deepseek-r1:70b), codex(deepseek-r1:70b), junie(deepseek-r1:70b), opencode(deepseek-r1:70b), pi(deepseek-r1:70b)
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(deepseek-r1:70b), pi(deepseek-r1:70b)
- **closer**: claude(deepseek-r1:70b)
- **env_bootstrap**: claude(deepseek-r1:70b), pi(deepseek-r1:70b)

## Totals

ERROR=24, FAIL_HARNESS=23, FAIL_MODEL=4, PASS=12
