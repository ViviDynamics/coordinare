# Persona benchmark — `deepcoder-14b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b |
| architect | ✅ deepcoder:14b | ✅ deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 💥 deepcoder:14b |
| implementer | ✅ deepcoder:14b | ✅ deepcoder:14b | 🔧 deepcoder:14b | ✅ deepcoder:14b | 🔧 deepcoder:14b | ✅ deepcoder:14b | ✅ deepcoder:14b |
| reviewer | ❌ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b |
| security | ❌ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b |
| qa | ❌ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b |
| tech_writer | ✅ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | ✅ deepcoder:14b |
| closer | ✅ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b |
| env_bootstrap | ✅ deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | 🔧 deepcoder:14b | 💥 deepcoder:14b | ✅ deepcoder:14b |

## Per-persona — which (backend, model) PASSed

- **assessor**: — none passed —
- **architect**: claude(deepcoder:14b), codex(deepcoder:14b)
- **implementer**: claude(deepcoder:14b), codex(deepcoder:14b), junie(deepcoder:14b), opencode(deepcoder:14b), pi(deepcoder:14b)
- **reviewer**: — none passed —
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: claude(deepcoder:14b), pi(deepcoder:14b)
- **closer**: claude(deepcoder:14b)
- **env_bootstrap**: claude(deepcoder:14b), pi(deepcoder:14b)

## Totals

ERROR=24, FAIL_HARNESS=23, FAIL_MODEL=4, PASS=12
