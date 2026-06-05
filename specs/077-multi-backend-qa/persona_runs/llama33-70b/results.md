# Persona benchmark — `llama33-70b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ✅ llama3.3:70b |
| architect | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b |
| implementer | 💥 llama3.3:70b | 💥 llama3.3:70b | 💥 llama3.3:70b | 💥 llama3.3:70b | 💥 llama3.3:70b | 💥 llama3.3:70b | 💥 llama3.3:70b |
| reviewer | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b |
| security | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b |
| qa | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b | ❌ llama3.3:70b |
| tech_writer | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ❌ llama3.3:70b | 🔧 llama3.3:70b | ❌ llama3.3:70b | ✅ llama3.3:70b |
| closer | ✅ llama3.3:70b | 🔧 llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ❌ llama3.3:70b | ✅ llama3.3:70b |
| env_bootstrap | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b | ✅ llama3.3:70b |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(llama3.3:70b)
- **architect**: claude(llama3.3:70b), codex(llama3.3:70b), hermes(llama3.3:70b), junie(llama3.3:70b), openclaw(llama3.3:70b), opencode(llama3.3:70b), pi(llama3.3:70b)
- **implementer**: — none passed —
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(llama3.3:70b), codex(llama3.3:70b), hermes(llama3.3:70b), pi(llama3.3:70b)
- **closer**: claude(llama3.3:70b), hermes(llama3.3:70b), junie(llama3.3:70b), openclaw(llama3.3:70b), pi(llama3.3:70b)
- **env_bootstrap**: claude(llama3.3:70b), codex(llama3.3:70b), hermes(llama3.3:70b), junie(llama3.3:70b), openclaw(llama3.3:70b), opencode(llama3.3:70b), pi(llama3.3:70b)

## Totals

ERROR=7, FAIL_HARNESS=7, FAIL_MODEL=25, PASS=24
