# Feature Specification: Self-Hosted Backend Robustness Layer

**Feature Branch**: `078-selfhosted-backend-shim` *(stub — to be formalized via `speckit.specify` from `main`)*
**Created**: 2026-05-31
**Status**: Draft (design stub — no implementation tasks yet)
**Input**: Design discussion during the 077 live round — "add a shim for each backend that's active when it's our own self-hosted infra running (as opposed to the agent's cloud API)."

## Overview

The 077 diverse-backend round surfaced a clear pattern: **almost every backend
failure was an artifact of *our self-hosted stack* (LiteLLM proxy → LM Studio /
Ollama → OSS models), not of the agent CLI itself.** Each agent works correctly
against its native cloud API; the failures appeared only when the backend was
re-pointed at our infra via its provider-override env.

This spec proposes a **self-hosted backend robustness layer**: a normalization +
routing + health-gating layer that activates **only when a backend is routed at
our self-hosted infra** (provider-override set) and is a transparent no-op when
the backend uses its native vendor cloud (where responses are already
well-formed). It generalizes the existing `ClaudeCodeShim` (073), which already
embodies this exact pattern (activates on `LITELLM_PROXY_BASE_URL`, translates
the response).

## Motivation — failure-mode catalog from 077 (all self-hosted-stack artifacts)

| Symptom (077) | Root cause | Current one-off fix |
|---|---|---|
| openclaw reviewer: "can't read files" / empty / garbage / `subprocess_exit:1` | LiteLLM's **streaming harmony→`tool_calls`** transform for gpt-oss leaks raw `<\|channel\|>commentary to=read …` text instead of structured `tool_calls` (LiteLLM #13300/#17246, openclaw PR #11210) | reroute openclaw → Ollama-direct (`gpt-oss:120b`), bypassing LiteLLM |
| claude_code thinking-block leakage | claude CLI surfaces `thinking` blocks from the proxy stream | `ClaudeCodeShim` (073) strips them (JSON + SSE) |
| hermes tech_writer can't connect | hermes-agent 0.15.2 config schema drift vs the backend's `providers:` block | per-backend config-schema fix (077) |
| codex implementer 62-min / 2-hr hangs | spark/qwen Ollama runner wedges; no client/role timeout | stall watchdog (077) + manual Ollama restart |
| claude_code qa `--max-tokens` crash | unsupported CLI flag for the self-hosted cap | env-var cap (077) |

Each was fixed ad-hoc. The thesis of 078: these belong to **one subsystem** —
the boundary between an agent CLI and our self-hosted model serving — and should
be handled there, once, rather than re-discovered per backend.

## Core design (sketch)

### 1. Activation: self-hosted detection
A backend is "self-hosted-routed" when its provider-override base URL is set
(`<BACKEND>_PROVIDER_BASE_URL` / `LITELLM_PROXY_BASE_URL` / `OPENCLAW_PROVIDER_BASE_URL`,
etc.) and points at our infra (LiteLLM / LM Studio / Ollama). Unset → native
vendor cloud → the layer is a no-op. This switch already exists in config.

### 2. Two strategies per backend/target (config-selectable)
- **Normalize (shim):** a loopback proxy (generalized `ClaudeCodeShim`) that
  forwards to the self-hosted upstream and **translates the response** through
  pluggable **normalizers keyed by response format / model-family**, not by
  agent — because the quirks are per-format and shared across agents:
  - `harmony → tool_calls` (gpt-oss; JSON + SSE reassembly) — the openclaw fix, generalized
  - `thinking/reasoning-block strip` (claude, qwen reasoners) — the 073 fix, generalized
  - streaming tool-call delta reassembly hardening
- **Reroute:** when a *clean* upstream exists (e.g. Ollama parses harmony→`tool_calls`
  correctly), point the backend directly at it and **skip the lossy middleware**
  (LiteLLM) entirely — lower latency, no translation. This is the openclaw→Ollama
  fix. **Key principle: a shim is not always the answer — sometimes the fix is
  "don't go through the broken layer."**

### 3. Health / smoke-test gating (pairs with this layer)
On container/daemon startup, smoke-test the resolved self-hosted path (trivial
connect + a tool-calling probe). Gate routing on the result so a broken path
fails fast / reroutes / surfaces a clear error, instead of black-holing a card
mid-lifecycle (cf. the 077 model-hang chase). See also the "backend health
gating" idea raised in 077.

## Functional requirements (design-level — to refine)

- **FR-078-1** The layer MUST be a no-op when a backend uses its native vendor cloud (provider-override unset).
- **FR-078-2** The shim MUST support pluggable normalizers keyed by response format/model-family, reusable across backends (DRY — one harmony normalizer serves all tool-using agents on gpt-oss).
- **FR-078-3** Normalizers MUST handle BOTH non-streaming JSON and SSE streams (the hard case; cf. `ClaudeCodeShim`'s stateful SSE filter).
- **FR-078-4** Each backend+self-hosted-target MUST be able to declare its strategy: `normalize` (via shim) or `reroute` (direct to a clean upstream).
- **FR-078-5** A startup smoke-test SHOULD validate the self-hosted tool-calling path and gate/route accordingly.
- **FR-078-6** Generalize the existing `ClaudeCodeShim` into the shared framework rather than adding N independent shims.

## Out of scope
- Fixing LiteLLM itself (upstream — tracked separately via #13300/#17246).
- Changing native-cloud behavior of any backend.
- The 077 stall watchdog (already landed) — though it composes with the health-gating piece.

## References
- 077 findings: `specs/077-multi-backend-qa/findings.md` (openclaw root cause + Ollama-direct fix; the full failure catalog).
- `agent/performer/src/performer/backends/claude_code_shim.py` (073) — the prototype.
- Upstream: BerriAI/litellm #13300, #17246; openclaw PR #11210.
