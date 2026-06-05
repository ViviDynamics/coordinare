# Persona benchmark — `llama31-70b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M |
| architect | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M |
| implementer | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M |
| reviewer | ❌ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M |
| security | ❌ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M |
| qa | ❌ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M |
| tech_writer | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ❌ llama3.1:70b-instruct-q4_K_M |
| closer | ✅ llama3.1:70b-instruct-q4_K_M | 🔧 llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M |
| env_bootstrap | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | 💥 llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M | ✅ llama3.1:70b-instruct-q4_K_M |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(llama3.1:70b-instruct-q4_K_M), codex(llama3.1:70b-instruct-q4_K_M), hermes(llama3.1:70b-instruct-q4_K_M), junie(llama3.1:70b-instruct-q4_K_M), openclaw(llama3.1:70b-instruct-q4_K_M), opencode(llama3.1:70b-instruct-q4_K_M), pi(llama3.1:70b-instruct-q4_K_M)
- **implementer**: — none passed —
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(llama3.1:70b-instruct-q4_K_M), codex(llama3.1:70b-instruct-q4_K_M), opencode(llama3.1:70b-instruct-q4_K_M)
- **closer**: claude(llama3.1:70b-instruct-q4_K_M), hermes(llama3.1:70b-instruct-q4_K_M), junie(llama3.1:70b-instruct-q4_K_M), openclaw(llama3.1:70b-instruct-q4_K_M), opencode(llama3.1:70b-instruct-q4_K_M), pi(llama3.1:70b-instruct-q4_K_M)
- **env_bootstrap**: claude(llama3.1:70b-instruct-q4_K_M), codex(llama3.1:70b-instruct-q4_K_M), hermes(llama3.1:70b-instruct-q4_K_M), openclaw(llama3.1:70b-instruct-q4_K_M), opencode(llama3.1:70b-instruct-q4_K_M), pi(llama3.1:70b-instruct-q4_K_M)

## Totals

ERROR=15, FAIL_HARNESS=7, FAIL_MODEL=19, PASS=22
