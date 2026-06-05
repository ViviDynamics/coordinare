# Persona benchmark — `glm47-flash`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |
| architect | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |
| implementer | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |
| reviewer | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |
| security | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | 🔧 glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest |
| qa | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest |
| tech_writer | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | 💥 glm-4.7-flash:latest | ❌ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ❌ glm-4.7-flash:latest |
| closer | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |
| env_bootstrap | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest | ✅ glm-4.7-flash:latest |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(glm-4.7-flash:latest)
- **architect**: claude(glm-4.7-flash:latest), codex(glm-4.7-flash:latest), hermes(glm-4.7-flash:latest), junie(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), opencode(glm-4.7-flash:latest), pi(glm-4.7-flash:latest)
- **implementer**: claude(glm-4.7-flash:latest), codex(glm-4.7-flash:latest), hermes(glm-4.7-flash:latest), junie(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), opencode(glm-4.7-flash:latest), pi(glm-4.7-flash:latest)
- **reviewer**: junie(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), pi(glm-4.7-flash:latest)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(glm-4.7-flash:latest), codex(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), opencode(glm-4.7-flash:latest)
- **closer**: claude(glm-4.7-flash:latest), codex(glm-4.7-flash:latest), hermes(glm-4.7-flash:latest), junie(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), opencode(glm-4.7-flash:latest), pi(glm-4.7-flash:latest)
- **env_bootstrap**: claude(glm-4.7-flash:latest), codex(glm-4.7-flash:latest), hermes(glm-4.7-flash:latest), junie(glm-4.7-flash:latest), openclaw(glm-4.7-flash:latest), opencode(glm-4.7-flash:latest), pi(glm-4.7-flash:latest)

## Totals

ERROR=1, FAIL_HARNESS=1, FAIL_MODEL=25, PASS=36
