# Persona benchmark — `shakeout2-120b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | codex | opencode |
|---|---|---|
| assessor | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| architect | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| implementer | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| reviewer | ❌ gpt-oss:120b | ✅ gpt-oss:120b |
| security | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| qa | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| tech_writer | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| closer | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| env_bootstrap | ✅ gpt-oss:120b | ✅ gpt-oss:120b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: codex(gpt-oss:120b), opencode(gpt-oss:120b)
- **implementer**: codex(gpt-oss:120b), opencode(gpt-oss:120b)
- **reviewer**: opencode(gpt-oss:120b)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: codex(gpt-oss:120b), opencode(gpt-oss:120b)
- **closer**: codex(gpt-oss:120b), opencode(gpt-oss:120b)
- **env_bootstrap**: codex(gpt-oss:120b), opencode(gpt-oss:120b)

## Totals

FAIL_MODEL=7, PASS=11
