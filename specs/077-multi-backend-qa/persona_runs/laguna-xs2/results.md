# Persona benchmark — `laguna-xs2`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | 🔧 laguna-xs.2:latest |
| architect | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest |
| implementer | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest |
| reviewer | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ✅ laguna-xs.2:latest |
| security | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | 🔧 laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest |
| qa | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ✅ laguna-xs.2:latest | 💥 laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ❌ laguna-xs.2:latest |
| tech_writer | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | 💥 laguna-xs.2:latest | 🔧 laguna-xs.2:latest | 🔧 laguna-xs.2:latest | ❌ laguna-xs.2:latest |
| closer | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | 💥 laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ❌ laguna-xs.2:latest | ✅ laguna-xs.2:latest |
| env_bootstrap | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest | ✅ laguna-xs.2:latest |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), hermes(laguna-xs.2:latest), junie(laguna-xs.2:latest), openclaw(laguna-xs.2:latest), opencode(laguna-xs.2:latest), pi(laguna-xs.2:latest)
- **implementer**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), hermes(laguna-xs.2:latest), junie(laguna-xs.2:latest), openclaw(laguna-xs.2:latest), opencode(laguna-xs.2:latest), pi(laguna-xs.2:latest)
- **reviewer**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), hermes(laguna-xs.2:latest), junie(laguna-xs.2:latest), pi(laguna-xs.2:latest)
- **security**: — none passed —
- **qa**: hermes(laguna-xs.2:latest)
- **tech_writer**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), hermes(laguna-xs.2:latest)
- **closer**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), junie(laguna-xs.2:latest), openclaw(laguna-xs.2:latest), pi(laguna-xs.2:latest)
- **env_bootstrap**: claude(laguna-xs.2:latest), codex(laguna-xs.2:latest), hermes(laguna-xs.2:latest), junie(laguna-xs.2:latest), openclaw(laguna-xs.2:latest), opencode(laguna-xs.2:latest), pi(laguna-xs.2:latest)

## Totals

ERROR=3, FAIL_HARNESS=4, FAIL_MODEL=21, PASS=35
