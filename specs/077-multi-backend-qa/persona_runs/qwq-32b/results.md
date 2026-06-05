# Persona benchmark — `qwq-32b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwq:32b | ✅ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ✅ qwq:32b |
| architect | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b |
| implementer | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b |
| reviewer | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b |
| security | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ✅ qwq:32b | ❌ qwq:32b |
| qa | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ❌ qwq:32b | ✅ qwq:32b | ❌ qwq:32b | 🔧 qwq:32b |
| tech_writer | ✅ qwq:32b | ✅ qwq:32b | 💥 qwq:32b | ❌ qwq:32b | 🔧 qwq:32b | ✅ qwq:32b | 🔧 qwq:32b |
| closer | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ❌ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b |
| env_bootstrap | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ❌ qwq:32b | ✅ qwq:32b | ✅ qwq:32b | ✅ qwq:32b |

## Per-persona — which (backend, model) PASSed

- **assessor**: codex(qwq:32b), pi(qwq:32b)
- **architect**: claude(qwq:32b), codex(qwq:32b), hermes(qwq:32b), junie(qwq:32b), openclaw(qwq:32b), opencode(qwq:32b), pi(qwq:32b)
- **implementer**: claude(qwq:32b), codex(qwq:32b), hermes(qwq:32b), junie(qwq:32b), openclaw(qwq:32b), opencode(qwq:32b), pi(qwq:32b)
- **reviewer**: — none passed —
- **security**: opencode(qwq:32b)
- **qa**: openclaw(qwq:32b)
- **tech_writer**: claude(qwq:32b), codex(qwq:32b), opencode(qwq:32b)
- **closer**: claude(qwq:32b), codex(qwq:32b), hermes(qwq:32b), openclaw(qwq:32b), opencode(qwq:32b), pi(qwq:32b)
- **env_bootstrap**: claude(qwq:32b), codex(qwq:32b), hermes(qwq:32b), openclaw(qwq:32b), opencode(qwq:32b), pi(qwq:32b)

## Totals

ERROR=1, FAIL_HARNESS=3, FAIL_MODEL=26, PASS=33
