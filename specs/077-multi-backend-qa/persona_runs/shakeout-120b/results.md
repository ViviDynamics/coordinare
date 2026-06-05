# Persona benchmark — `shakeout-120b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | codex | opencode |
|---|---|---|
| assessor | 🔧 gpt-oss:120b | 🔧 gpt-oss:120b |
| architect | 🔧 gpt-oss:120b | 🔧 gpt-oss:120b |
| implementer | 💥 gpt-oss:120b | 💥 gpt-oss:120b |
| reviewer | ❌ gpt-oss:120b | ✅ gpt-oss:120b |
| security | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| qa | ❌ gpt-oss:120b | 🔧 gpt-oss:120b |
| tech_writer | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| closer | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| env_bootstrap | 🔧 gpt-oss:120b | 🔧 gpt-oss:120b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: — none passed —
- **implementer**: — none passed —
- **reviewer**: opencode(gpt-oss:120b)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: — none passed —
- **closer**: codex(gpt-oss:120b), opencode(gpt-oss:120b)
- **env_bootstrap**: — none passed —

## Totals

ERROR=2, FAIL_HARNESS=7, FAIL_MODEL=6, PASS=3
