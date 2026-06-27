# Functional Spread Results (Spec 122 US2 — wire-level)

**Date**: 2026-06-26 · **Gateway**: https://litellm.vividynamics.com · **Tool**: `scripts/litellm_spread.py`

A fast wire-level spread (no containers) run after the LiteLLM + Ollama update, probing each routed
model for the capabilities the orchestrator's per-backend workarounds were built to patch. This is the
gateway-side half of US2; the per-backend *container* matrix (each CLI's integration) remains as
US2's full verification, but the hard wire pathologies are settled here.

## Matrix

| Model (`spark/…`) | served | tool_calls (structured) | reasoning separated | control-clean | `/v1/messages` |
|---|:--:|:--:|:--:|:--:|:--:|
| gpt-oss:120b | ✅ | ✅ | ✅ | ✅ | ✅ |
| gpt-oss:20b | ✅ | ✅ | ✅ | ✅ | — |
| qwen3-coder:30b | ✅ | ✅ | n/a (non-reasoning) | ✅ | — |
| glm-4.7-flash:latest | ✅ | ✅ | ✅ | ✅ | — |
| qwen2.5-coder:14b-instruct-q6_K | ✅ | ⚠️ **leaks into content** | n/a | ✅ | — |
| qwen3.6:35b | ✅ | ✅ | ✅ | ✅ | — |

(gpt-oss:120b is slow — needs ~240s timeout, hence the longer default in the tool; an initial 90s probe
timed out but the model is clean.)

## Workaround verdicts

| Workaround (normalizer/strategy) | Status | Evidence |
|---|---|---|
| `harmony_tool_calls` | **OBSOLETE** | gpt-oss:120b/:20b return structured `tool_calls` |
| `strip_reasoning` | **OBSOLETE** | reasoning lands in `reasoning_content`, `content` is clean |
| `strip_control_chars` | **OBSOLETE** | every model's `content` is control-char clean |
| `translate` (Anthropic→OpenAI, claude_code) | **OBSOLETE** | gpt-oss:120b answers on `/v1/messages` |
| `reroute` / **Ollama-direct bypass** | **OBSOLETE** | LiteLLM now serves *all* routed models, incl. the formerly-"dead" `spark/*` coders |

## The one caveat to carry into US3

`spark/qwen2.5-coder:14b-instruct-q6_K` still **leaks tool calls into `content`** (no structured
`tool_calls`). Its only fleet user is **env_bootstrap**, which already runs it via `translate` with
**no** tool-call normalizer (treated as a plain coder), so this is a model limitation, not a regressed
workaround. **Constraint for the migration:** no *tool-using* role may be pointed at qwen2.5-coder; a
tool-using coder role should use `qwen3-coder:30b` (structured tool_calls ✅).

## Implication for US3

Drop all four normalizer/strategy classes (`harmony_tool_calls`, `strip_reasoning`,
`strip_control_chars`, `translate`) **and** the `reroute`/Ollama-direct entries — they are redundant
through the updated gateway. The only residual nuance is the qwen2.5-coder tool-call constraint above.
The per-backend container matrix (US2 full) confirms each CLI integrates; this spread confirms the
gateway no longer needs the shims.

## Reproduce

```
scripts/litellm_spread.py                       # the routed set, 240s timeout
scripts/litellm_spread.py --models spark/gpt-oss:120b   # one model
```
Artifact: `tmp/litellm_spread.json`.

---

# Container Matrix Results (US2 full — per-backend via LiteLLM)

**Date**: 2026-06-26 · Run one-per-backend via `scripts/smoke_backends.py --via-litellm --matrix`
against `config.example.litellm-test.yaml` (each backend → LiteLLM `spark/gpt-oss:120b`, no shim).
Compatibility = the performer emitted a valid QA terminal status (qa_passed/qa_failed/qa_env_blocked)
⇒ it reached the model through the gateway and produced its role contract. (`qa_env_blocked` here is
just the Ruby-less smoke env — a task concern, not a wire/model concern.)

| Backend | Verdict | Performer status / reason | Note |
|---|---|---|---|
| **claude_code** | ✅ compatible | `qa_env_blocked` | via `/v1/messages`, **no translate shim** |
| **codex** | ✅ compatible | `qa_failed` (unsubstantiated-pass refusal) | OpenAI `/v1`; `/responses` worry unfounded |
| **opencode** | ✅ compatible | `qa_env_blocked` | OpenAI `/v1` |
| **openclaw** | ✅ compatible | `qa_env_blocked` | OpenAI `/v1` |
| junie | ❌ incompatible | junie-internal: "Failed to build 'issue.md.junie_standalone'" | fails BEFORE the model — junie-harness issue, not gateway |
| pi | ❌ incompatible | `BACKEND_FORMAT_ERROR: output empty after 2 attempts` | empty content — likely reasoning eats token budget / pi doesn't read `reasoning_content`; try higher max_tokens |
| hermes | ❌ incompatible | `subprocess_exit:1` (after adding `HERMES_PROVIDER=litellm`) | hermes-CLI issue; needs deeper look |

## Disposition

- **Migrate now (US3):** claude_code, codex, opencode, openclaw — all proven gateway-compatible with
  **no shim**. Combined with the wire-spread (gateway handles tool_calls/reasoning/control/anthropic),
  the `harmony_tool_calls`/`strip_reasoning`/`strip_control_chars`/`translate`/`reroute` shims are
  redundant for these.
- **Needs US1 follow-up before migrating:** junie (own-harness build error), pi (empty output —
  max_tokens/reasoning), hermes (CLI exit 1). These are **backend-CLI** issues, NOT gateway/wire
  issues (the wire-spread already proved the gateway is clean). Per FR-013 they stay on current
  routing until fixed.

## Wiring that works (US1/T010 — source of truth for the live migration)

- claude_code → `ANTHROPIC_BASE_URL` = bare origin (CLI appends `/v1/messages`), `ANTHROPIC_AUTH_TOKEN` = master key.
- codex/opencode/openclaw → `*_PROVIDER_BASE_URL` = `${LITELLM_BASE_URL}` (already `/v1`), `OPENAI_API_KEY` = master key.
- model = `spark/gpt-oss:120b` (NOT bare `gpt-oss:120b` — not served).

---

# qa observe cutover — VALIDATED LIVE (2026-06-26)

qa migrated to `claude_code → observe-passthrough → LiteLLM spark/gpt-oss:120b`. End-to-end probe
(diagnostic via live routing.yaml) confirmed: `selfhosted_layer.observe_launched` + `selfhosted_shim.request`
(coordinare-side tap live), traffic to `litellm.vividynamics.com`, **zero** `192.168.3.30` (Ollama), and
`state: succeeded`. Two real gaps were found + fixed to get here (both committed, image rebuilt):
1. `observe` strategy was rejected by the deployed image → rebuilt with the spec-122 code.
2. the 078 health probe sent no auth (LiteLLM 401) and used the tiny tool_call budget (120s timeout on
   the slow reasoning model) → probe now sends Bearer auth + the entry uses `health_probe: completion`.
The prior Ollama-direct translate entry is kept commented for instant revert; backups at /tmp/*.bak.122.

---

# Observe-Cutover Container Findings (Spec 122 US3 — per-backend integration)

**Date**: 2026-06-26 · **Tool**: `scripts/smoke_backends.py` (now mounts routing.yaml + captures
container logs) · **Path**: each CLI → loopback shim (`strategy: observe`/`normalize`) → LiteLLM.

The wire-spread (above) settles the *gateway* half. This is the *integration* half: does each backend
CLI actually drive LiteLLM-served models through the shim. It diverges from the wire-spread because a
CLI's model-id construction and a CLI's JSON parser are exercised here, not just raw `/v1/...`.

| Role(s) | Backend | Result via LiteLLM | Disposition |
|---|---|---|---|
| qa | claude_code | ✅ observe→`spark/gpt-oss:120b` (anthropic wire) — live | **migrated** |
| architect, implementer | codex | ✅ already DIRECT→LiteLLM (`spark/qwen3.6:35b`, `CODEX_PROVIDER_BASE_URL`) | **migrated** (no shim) |
| reviewer, security | openclaw | ⛔ shim+probe OK, but the openclaw CLI sends the wire model as `{provider}/{model}` → `sparkollama/spark/gpt-oss:120b` → LiteLLM **404 model_not_found** | **stays on Ollama** (FR-013) |
| env_bootstrap | opencode | ⛔ LiteLLM returns **invalid JSON** for `spark/glm-4.7-flash:latest` — an unescaped control char inside `reasoning_content` breaks the parse (probe *and* CLI). `strip_control_chars` can't help: the normalizer runs after the JSON parse, which has already failed | **stays on Ollama** (FR-013) |
| assessor | junie | (not retried) | stays on Ollama |
| tech_writer | hermes | (not retried) | stays on Ollama |
| closer | pi | (not retried) | stays on Ollama |

## Two concrete blockers (US1 follow-ups)

1. **openclaw provider-prefix 404.** `OpenClawBackend` writes a custom provider and dispatches the wire
   model as `{OPENCLAW_PROVIDER_NAME}/{model}`. No config combination resolves it: the **probe** body
   uses the routing-entry model, which must be the LiteLLM-served `spark/gpt-oss:120b` (else the probe
   404s) — but openclaw then prefixes its provider name onto that, producing `sparkollama/spark/…` on
   the real request. Fix is in the backend: emit the bare served model id on the wire (don't
   double-prefix), or give the probe a separate `upstream_model` for the openai/observe path.

2. **glm-4.7-flash invalid JSON from LiteLLM.** `spark/glm-4.7-flash:latest` responses carry a raw
   (unescaped) control character in `reasoning_content`, so the whole response is not valid JSON. This
   is a gateway/model-serving defect, fixable only upstream (LiteLLM/Ollama) — not by a coordinare
   normalizer (it cannot parse the body either). `spark/gpt-oss:120b` returns valid JSON, so an option
   (a model-tier decision for the sensitive env_bootstrap role) is to move env_bootstrap to
   `gpt-oss:120b` via observe instead of glm.

**Net:** qa (shim) + codex (direct) are on LiteLLM; openclaw + opencode are blocked by real
backend/gateway defects, not configuration. The wire-spread's "compatible 4/7" was the gateway ceiling;
the integration floor is qa + codex until the two blockers land.

---

# Gateway-Bug Fixes — Full Fleet on LiteLLM (Spec 122 US1, 2026-06-27)

Isolated every per-backend failure with `scripts/wire_capture.py` (a forward proxy
that logs the exact wire model / path / auth header a CLI sends + the raw upstream
response). **Finding: the LiteLLM gateway itself was healthy throughout** — every
failure was a coordinare/shim/CLI integration bug. All fixed; **7/7 backends now
reach LiteLLM and emit valid role contracts.**

| Role(s) | Backend | Was | Root cause | Fix |
|---|---|---|---|---|
| qa | claude_code | ✅ | — | observe shim (anthropic wire) |
| architect/implementer | codex | ✅ | — | direct (CODEX_PROVIDER_BASE_URL) |
| reviewer/security | openclaw | 404 | observe-shim hop 404'd its OpenAI-wire request | route **direct** |
| env_bootstrap | opencode | invalid-JSON / 404 | glm intermittent control byte + reasoning-only content; shim only routed /v1 paths | normalize (control+reasoning) + shim path canonicalize + probe raw-normalize |
| closer | pi | 401 | CLI emits unresolved `${LITELLM_MASTER_KEY}` on the wire | shim `upstream_auth_env` injection |
| tech_writer | hermes | 401 | CLI sends key via `api-key` header = `no-key-required` | inject auth + drop alternate auth headers |
| assessor | junie | own-harness / 400 | sent no auth, then bare model `gpt-oss:120b` (LiteLLM 400) | auth injection + `JUNIE_PROVIDER_MODEL=spark/gpt-oss:120b` |

## Coordinare code fixes (committed; 288 proxy tests green)

1. **`health.check_health`** — apply each declared normalizer's `normalize_raw`
   (pre-parse) before `json.loads`, so an unescaped control byte no longer
   fail-closes a healthy model (the shim already did this for traffic; the probe
   didn't).
2. **`shim` front-door paths** — register `/chat/completions` and `/messages`
   (no-`/v1`) and canonicalize to the upstream `/v1` path. A CLI whose repointed
   base_url lacks `/v1` POSTs the short form; it used to 404 at the router. Shared
   cause of the openclaw + opencode shim failures.
3. **`shim` `upstream_auth_env`** (new `TargetDescriptor` field) — the shim
   overrides the forwarded `Authorization` (and drops `api-key`/`x-api-key`) with
   `Bearer ${env}`. **Use `OPENAI_API_KEY`**, not `LITELLM_MASTER_KEY`: the latter
   is scrubbed from the shim's process env; `OPENAI_API_KEY` (set = master on each
   endpoint) survives.

## Config (live, gitignored)

- openclaw → direct LiteLLM (drop routing entry + SELFHOSTED). reviewer/security
  mode → `single-gptoss120-spark`.
- opencode env_bootstrap → `single-glm47flash-spark` + normalize entry
  (strip_control_chars, strip_reasoning).
- junie/hermes/pi → normalize entries with `upstream_auth_env: OPENAI_API_KEY`;
  endpoints get the routing.yaml mount + `OPENAI_API_KEY` (probe + injection).
  `JUNIE_PROVIDER_MODEL` → `spark/gpt-oss:120b`.

Deployed live 2026-06-27; daemon healthy. Backups `/tmp/{config,routing}.yaml.bak.122c`.
