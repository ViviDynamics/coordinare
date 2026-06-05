# Persona benchmark — `qwen25-14b`

_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._

| persona | claude | codex | hermes | junie | openclaw | opencode | pi |
|---|---|---|---|---|---|---|---|
| assessor | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| architect | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| implementer | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| reviewer | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| security | ❌ qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K |
| qa | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K |
| tech_writer | ❌ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | 💥 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | 🔧 qwen2.5:14b-instruct-q6_K | ❌ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| closer | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |
| env_bootstrap | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K | ✅ qwen2.5:14b-instruct-q6_K |

## Per-persona — which (backend, model) PASSed

- **assessor**: pi(qwen2.5:14b-instruct-q6_K)
- **architect**: claude(qwen2.5:14b-instruct-q6_K), codex(qwen2.5:14b-instruct-q6_K), hermes(qwen2.5:14b-instruct-q6_K), junie(qwen2.5:14b-instruct-q6_K), openclaw(qwen2.5:14b-instruct-q6_K), opencode(qwen2.5:14b-instruct-q6_K), pi(qwen2.5:14b-instruct-q6_K)
- **implementer**: claude(qwen2.5:14b-instruct-q6_K), codex(qwen2.5:14b-instruct-q6_K), hermes(qwen2.5:14b-instruct-q6_K), junie(qwen2.5:14b-instruct-q6_K), openclaw(qwen2.5:14b-instruct-q6_K), opencode(qwen2.5:14b-instruct-q6_K), pi(qwen2.5:14b-instruct-q6_K)
- **reviewer**: pi(qwen2.5:14b-instruct-q6_K)
- **security**: — none passed —
- **qa**: — none passed —
- **tech_writer**: codex(qwen2.5:14b-instruct-q6_K), pi(qwen2.5:14b-instruct-q6_K)
- **closer**: claude(qwen2.5:14b-instruct-q6_K), codex(qwen2.5:14b-instruct-q6_K), hermes(qwen2.5:14b-instruct-q6_K), junie(qwen2.5:14b-instruct-q6_K), openclaw(qwen2.5:14b-instruct-q6_K), opencode(qwen2.5:14b-instruct-q6_K), pi(qwen2.5:14b-instruct-q6_K)
- **env_bootstrap**: claude(qwen2.5:14b-instruct-q6_K), codex(qwen2.5:14b-instruct-q6_K), hermes(qwen2.5:14b-instruct-q6_K), junie(qwen2.5:14b-instruct-q6_K), openclaw(qwen2.5:14b-instruct-q6_K), opencode(qwen2.5:14b-instruct-q6_K), pi(qwen2.5:14b-instruct-q6_K)

## Totals

ERROR=6, FAIL_HARNESS=8, FAIL_MODEL=17, PASS=32
