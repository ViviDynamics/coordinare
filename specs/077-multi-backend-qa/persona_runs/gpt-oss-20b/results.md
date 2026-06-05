# Persona benchmark — `gpt-oss-20b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | 💥 gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ✅ gpt-oss:20b |
| architect | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b |
| implementer | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b |
| reviewer | ✅ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ✅ gpt-oss:20b | ❌ gpt-oss:20b | ✅ gpt-oss:20b | ❌ gpt-oss:20b |
| security | ❌ gpt-oss:20b | ❌ gpt-oss:20b | 🔧 gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b |
| qa | ✅ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | 💥 gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b | ❌ gpt-oss:20b |
| tech_writer | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | 🔧 gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b |
| closer | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | 💥 gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b |
| env_bootstrap | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b | ✅ gpt-oss:20b |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(gpt-oss:20b)
- **architect**: claude(gpt-oss:20b), codex(gpt-oss:20b), hermes(gpt-oss:20b), junie(gpt-oss:20b), openclaw(gpt-oss:20b), opencode(gpt-oss:20b), pi(gpt-oss:20b)
- **implementer**: claude(gpt-oss:20b), codex(gpt-oss:20b), hermes(gpt-oss:20b), junie(gpt-oss:20b), openclaw(gpt-oss:20b), opencode(gpt-oss:20b), pi(gpt-oss:20b)
- **reviewer**: claude(gpt-oss:20b), junie(gpt-oss:20b), opencode(gpt-oss:20b)
- **security**: — none passed —
- **qa**: claude(gpt-oss:20b)
- **tech_writer**: claude(gpt-oss:20b), codex(gpt-oss:20b), hermes(gpt-oss:20b), openclaw(gpt-oss:20b), opencode(gpt-oss:20b), pi(gpt-oss:20b)
- **closer**: claude(gpt-oss:20b), codex(gpt-oss:20b), hermes(gpt-oss:20b), openclaw(gpt-oss:20b), opencode(gpt-oss:20b), pi(gpt-oss:20b)
- **env_bootstrap**: claude(gpt-oss:20b), codex(gpt-oss:20b), hermes(gpt-oss:20b), junie(gpt-oss:20b), openclaw(gpt-oss:20b), opencode(gpt-oss:20b), pi(gpt-oss:20b)

## Totals

ERROR=3, FAIL_HARNESS=2, FAIL_MODEL=20, PASS=38
