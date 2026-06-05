# Persona benchmark — `gpt-oss-120b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ✅ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ✅ gpt-oss:120b |
| architect | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| implementer | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| reviewer | ✅ gpt-oss:120b | ❌ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ❌ gpt-oss:120b |
| security | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ✅ gpt-oss:120b |
| qa | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b | ❌ gpt-oss:120b |
| tech_writer | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | 💥 gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| closer | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b |
| env_bootstrap | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b | ✅ gpt-oss:120b |

## Per-persona — which (backend, model) PASSed

- **assessor**: junie(gpt-oss:120b), pi(gpt-oss:120b)
- **architect**: claude(gpt-oss:120b), codex(gpt-oss:120b), hermes(gpt-oss:120b), junie(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b), pi(gpt-oss:120b)
- **implementer**: claude(gpt-oss:120b), codex(gpt-oss:120b), hermes(gpt-oss:120b), junie(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b), pi(gpt-oss:120b)
- **reviewer**: claude(gpt-oss:120b), hermes(gpt-oss:120b), junie(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b)
- **security**: pi(gpt-oss:120b)
- **qa**: — none passed —
- **tech_writer**: claude(gpt-oss:120b), codex(gpt-oss:120b), hermes(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b), pi(gpt-oss:120b)
- **closer**: claude(gpt-oss:120b), codex(gpt-oss:120b), hermes(gpt-oss:120b), junie(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b), pi(gpt-oss:120b)
- **env_bootstrap**: claude(gpt-oss:120b), codex(gpt-oss:120b), hermes(gpt-oss:120b), junie(gpt-oss:120b), openclaw(gpt-oss:120b), opencode(gpt-oss:120b), pi(gpt-oss:120b)

## Totals

ERROR=1, FAIL_MODEL=20, PASS=42
