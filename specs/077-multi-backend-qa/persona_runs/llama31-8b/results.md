# Persona benchmark — `llama31-8b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 |
| architect | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 |
| implementer | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 |
| reviewer | ❌ llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 |
| security | ❌ llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 |
| qa | ❌ llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 | ❌ llama3.1:8b-instruct-q4_0 |
| tech_writer | ❌ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 |
| closer | ✅ llama3.1:8b-instruct-q4_0 | 🔧 llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 |
| env_bootstrap | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | 💥 llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 | ✅ llama3.1:8b-instruct-q4_0 |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(llama3.1:8b-instruct-q4_0), codex(llama3.1:8b-instruct-q4_0), hermes(llama3.1:8b-instruct-q4_0), openclaw(llama3.1:8b-instruct-q4_0), opencode(llama3.1:8b-instruct-q4_0), pi(llama3.1:8b-instruct-q4_0)
- **implementer**: — none passed —
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: codex(llama3.1:8b-instruct-q4_0), hermes(llama3.1:8b-instruct-q4_0), openclaw(llama3.1:8b-instruct-q4_0), opencode(llama3.1:8b-instruct-q4_0), pi(llama3.1:8b-instruct-q4_0)
- **closer**: claude(llama3.1:8b-instruct-q4_0), hermes(llama3.1:8b-instruct-q4_0), openclaw(llama3.1:8b-instruct-q4_0), opencode(llama3.1:8b-instruct-q4_0), pi(llama3.1:8b-instruct-q4_0)
- **env_bootstrap**: claude(llama3.1:8b-instruct-q4_0), codex(llama3.1:8b-instruct-q4_0), hermes(llama3.1:8b-instruct-q4_0), openclaw(llama3.1:8b-instruct-q4_0), opencode(llama3.1:8b-instruct-q4_0), pi(llama3.1:8b-instruct-q4_0)

## Totals

ERROR=18, FAIL_HARNESS=7, FAIL_MODEL=16, PASS=22
