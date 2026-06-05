# Persona benchmark — `mistral-instruct`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct | 💥 mistral:instruct | ❌ mistral:instruct | ✅ mistral:instruct | ❌ mistral:instruct |
| architect | ✅ mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct | 💥 mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct |
| implementer | 💥 mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct |
| reviewer | ❌ mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct | 💥 mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct |
| security | ❌ mistral:instruct | ❌ mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | 🔧 mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct |
| qa | ❌ mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct | 💥 mistral:instruct | 🔧 mistral:instruct | ❌ mistral:instruct | ❌ mistral:instruct |
| tech_writer | ❌ mistral:instruct | 🔧 mistral:instruct | ❌ mistral:instruct | 💥 mistral:instruct | ✅ mistral:instruct | 🔧 mistral:instruct | 🔧 mistral:instruct |
| closer | ✅ mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct | 💥 mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct |
| env_bootstrap | ✅ mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct | 💥 mistral:instruct | 💥 mistral:instruct | ✅ mistral:instruct | ✅ mistral:instruct |

## Per-persona — which (backend, model) PASSed

- **assessor**: opencode(mistral:instruct)
- **architect**: claude(mistral:instruct), codex(mistral:instruct), hermes(mistral:instruct), openclaw(mistral:instruct), opencode(mistral:instruct), pi(mistral:instruct)
- **implementer**: — none passed —
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: openclaw(mistral:instruct)
- **closer**: claude(mistral:instruct), codex(mistral:instruct), hermes(mistral:instruct), openclaw(mistral:instruct), opencode(mistral:instruct), pi(mistral:instruct)
- **env_bootstrap**: claude(mistral:instruct), codex(mistral:instruct), hermes(mistral:instruct), opencode(mistral:instruct), pi(mistral:instruct)

## Totals

ERROR=17, FAIL_HARNESS=5, FAIL_MODEL=22, PASS=19
