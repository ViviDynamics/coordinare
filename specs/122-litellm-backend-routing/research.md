# Research: Consolidate All Agent Backends on the LiteLLM Gateway (Spec 122)

Records the live probes run during scoping (2026-06-26) and the per-backend migration analysis, so the
plan/tasks build on verified facts rather than re-deriving them.

## Verified feasibility (live probes against https://litellm.vividynamics.com)

**Decision: the target catalog is `spark/gpt-oss:120b` (+ `spark/gpt-oss:20b` for lighter roles).**

- `spark/gpt-oss:120b` and `spark/gpt-oss:20b` are **healthy deployments** on LiteLLM. The **bare**
  `gpt-oss:120b` / `local/gpt-oss:120b` / `gpt-oss-120b` all return `no healthy deployments` — the
  working name carries the `spark/` prefix. (Operator added them server-side; this spec assumes that.)
- **OpenAI front door** (`POST /v1/chat/completions`, `model=spark/gpt-oss:120b`): returns clean
  `content` (e.g. `"HELLO"`), separates reasoning into a distinct **`reasoning_content`** field (not
  leaked into `content`), and on a tools request returns **structured `tool_calls`**
  (`get_weather {"city":"Paris"}`, `finish_reason=tool_calls`, no harmony text in `content`).
- **Anthropic front door** (`POST /v1/messages`, same model): returns a valid Anthropic message (with a
  `thinking` block) — so `claude_code` (Anthropic wire) can point `ANTHROPIC_BASE_URL` straight at
  LiteLLM with **no orchestrator translate shim**.
- **Implication:** the two pathologies that forced the Ollama-direct bypass — broken
  harmony→tool_calls and the reasoning-channel leak — are now handled **server-side by LiteLLM**. The
  orchestrator `harmony_tool_calls` / `strip_reasoning` normalizers are *expected redundant* on the
  LiteLLM path — **confirmed per backend by the US2 matrix, not assumed.**
- **Caveat:** at very low `max_tokens` (≤10) `content` can come back empty because reasoning consumes
  the budget first; the matrix and per-role config must allow adequate tokens.

**Rationale**: the operator's directive is to run the fleet on LiteLLM-served models; `spark/gpt-oss`
is what LiteLLM serves for the self-hosted tier and it already round-trips both wire formats correctly.

**Alternatives considered**:
- *LiteLLM `local/qwen3-14b`/`-8b`* (also served): weaker tier; qwen has known JSON-contract gaps for
  some roles ([[project-qwen-coder-limitations]]). Rejected as the default; available if a role needs it.
- *Keep Ollama-direct*: rejected — the whole point is consolidation; LiteLLM now does the repairs.

## Decision 2 (US1): how each backend points at LiteLLM

The lever already exists — `proxy/launch.py:PROVIDER_BASE_URL_ENV` maps each backend to its provider
base-URL env var. Migration = set that env to the LiteLLM proxy + the spec-080 `model_endpoints`/`modes`
to `spark/gpt-oss:120b`, with the LiteLLM master key as provider auth (via the redacted secret channel).

| Backend | Provider base-URL env | LiteLLM front door | Wire |
|---|---|---|---|
| claude_code | `ANTHROPIC_BASE_URL` | `/v1/messages` | Anthropic (no translate shim) |
| codex | `CODEX_PROVIDER_BASE_URL` | `/v1/chat/completions` | OpenAI |
| opencode / opencode_compat | `OPENCODE_PROVIDER_BASE_URL` | `/v1/chat/completions` | OpenAI |
| junie | `JUNIE_PROVIDER_BASE_URL` | `/v1/chat/completions` | OpenAI |
| pi | `PI_PROVIDER_BASE_URL` | `/v1/chat/completions` | OpenAI |
| openclaw | `OPENCLAW_PROVIDER_BASE_URL` | `/v1/chat/completions` | OpenAI |
| hermes | `HERMES_BASE_URL` | `/v1/chat/completions` | OpenAI |

`claude_code` already has the seam to suppress its own LiteLLM shim so it doesn't double-proxy
(`launch.py`); pointing `ANTHROPIC_BASE_URL` at LiteLLM `/v1/messages` is the clean path.

## Decision 3 (US2): the compatibility matrix harness

**Decision: extend `scripts/smoke_backends.py`** (it already launches each backend in its own ephemeral
container, POSTs a real `qa` job, and reads launched/state/output/verdict-parseable). Add a mode that
points each backend at LiteLLM `spark/gpt-oss:120b` (provider base-URL + model + master-key auth, **no**
`SELFHOSTED_ROUTING_CONFIG` so it hits LiteLLM directly, not the Ollama shim). Emit, per backend:
`launched`, `completed`, `output_present`, `contract_satisfied`, and a **normalizer-needed** column
(re-run with the candidate normalizer removed; if it still passes → redundant).

**Rationale**: reuse the proven harness; it already encodes the "real container, real job, parse the
verdict" pattern. A separate test-only `config` variant (via `--config`) keeps the live fleet untouched
(FR-008). Distinguish a gateway 5xx/"no such model" from a backend defect (FR-007).

**Alternatives**: wire-level curl probes only (done for feasibility, but they don't exercise each CLI's
own config/auth integration); a brand-new harness (rejected — duplicates smoke_backends).

## Decision 4 (US3): what to migrate / retire / delete

**Re-point to LiteLLM `spark/gpt-oss:120b`/`:20b`:**
- `routing.yaml` (5 entries, all Ollama-direct `192.168.3.30:11434`): claude_code-qa (translate),
  openclaw (reroute), junie (normalize), hermes (normalize), env_bootstrap (translate). For each
  backend the matrix marks gateway-compatible, either (a) remove the routing entry entirely (LiteLLM
  reached directly via provider base-URL + model, no shim), or (b) if a normalizer is still needed,
  re-point the entry's `base_url` to LiteLLM and keep only that normalizer.
- `model_endpoints`: point the self-hosted ones at LiteLLM `spark/gpt-oss:120b`/`:20b`.

**Delete (dead/stale):**
- `spark/*` self-hosted refs that the LiteLLM `spark/*` namespace no longer serves:
  `gptoss120-spark`, `gptoss20-spark` (now superseded by the working `spark/gpt-oss:*`),
  `qwen36` (`spark/qwen3.6:35b`), `qwen25coder` (`spark/qwen2.5-coder`).
- Ollama-direct `model_endpoints` that become unused after migration (`gptoss120-ollama`,
  `qwen3coder30-ollama`, `glm47flash-ollama`) — delete the ones no mode references post-migration.
- **`studio/*`: nothing to remove** — confirmed zero references in coordinare config; "studio" appears
  only as "LM Studio" in docs/examples. (Operator removed `studio/*` on the LiteLLM server side.)

**Keep:** `claude-sonnet` / frontier `spark/*` where a role intentionally wants frontier; any normalizer
the matrix proves a backend still needs.

**Rationale**: consolidate on one gateway + one served model name; remove the indirection (shims) the
gateway now obsoletes; delete refs that can no longer resolve.

## Functional spread (US2 gateway-side) — confirmed 2026-06-26

A wire-level spread (`scripts/litellm_spread.py`) across all routed models, run after the LiteLLM +
Ollama update, confirms the gateway-side fix — full matrix in
[compatibility-matrix-results.md](./compatibility-matrix-results.md). Verdict: **`harmony_tool_calls`,
`strip_reasoning`, `strip_control_chars`, `translate`, and the `reroute`/Ollama-direct bypass are ALL
obsolete** — gpt-oss:120b/:20b return structured `tool_calls`, separate reasoning into
`reasoning_content`, emit control-clean content, and gpt-oss:120b answers on `/v1/messages`; and the
formerly-"dead" `spark/*` coders (qwen3-coder:30b, glm-4.7-flash, qwen2.5-coder, qwen3.6:35b) are all
served now. **One residual constraint**: `qwen2.5-coder:14b` still leaks tool calls into `content` (no
structured tool_calls) — fine for its non-tool `env_bootstrap` use via `translate`, but no *tool-using*
role may be pointed at it (use `qwen3-coder:30b` instead).

## Decision 6: shims become an observe/passthrough (configurable), not deleted

The shim layer does TWO jobs: **repair** (normalizers/translate — now redundant, the spread proved
LiteLLM does it server-side) and **observability** (the `selfhosted_shim.request` per-request
latency/status logs + the `LITELLM_PROXY_CAPTURE_DIR` payload capture, in the coordinare's own
container — we relied on exactly this for the gpt-oss latency numbers + capture forensics this session).
Going fully LiteLLM-direct would drop the coordinare-side request tap + local capture.

**Decision (operator-chosen): make it configurable.**
- Default for LiteLLM routing: an **observe/passthrough** proxy — forwards to LiteLLM, applies NO
  transforms, but still logs latency/status + writes `capture_dir`. (The existing `normalize`
  strategy with `normalizers: []` is ~this; add an explicit `observe`/passthrough notion + a toggle.)
- A toggle (config flag, e.g. `selfhosted.observe_passthrough` / per-target `strategy: direct`) lets
  an operator switch a target to **direct** (no proxy hop) when they don't want the tap.
- The **repair normalizers stay in code, registered-but-unwired** (tests retained) → re-enabling one
  if LiteLLM regresses is a routing-config change, not a code rewrite. Nothing is deleted from the
  registry; the dead `model_endpoints` / Ollama-direct *entries* are still removed.

**Rationale**: keeps the coordinare-side observability the operator values, drops only the redundant
repair, preserves instant re-enablement, and a localhost passthrough hop is negligible vs. model
latency. Configurability avoids forcing one trade-off fleet-wide.

## Decision 5: removing normalizers safely

A normalizer is removed from the **registry/code** only if NO migrated backend needs it (per the
matrix). If some non-migrated or specific-backend path still needs it, it stays in the registry and is
simply dropped from the migrated routing entries. The existing self-hosted-layer + normalizer unit
tests are the regression guard; they must stay green after any removal.

## Cross-cutting

- **Secret invariant**: the LiteLLM master key flows only through the existing redacted secret channel
  (job secrets / env); matrix output + logs carry names/counts/reasons, never the key.
- **No new dependency**; **no persisted state**; the LiteLLM server config is operator-owned/out of scope.
- **Committed vs operational**: live `config.yaml`/`routing.yaml` are gitignored (edited operationally);
  the committed surface is `smoke_backends.py`, the `config.example.*`/`routing.example.yaml`, and any
  shim/normalizer code removed (with tests). Keep examples in sync with the real topology.
