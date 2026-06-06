# Phase 0 Research: Self-Hosted Backend Robustness Layer

All three open decision points were resolved during `/speckit.clarify` (spec
Session 2026-06-05). No `[NEEDS CLARIFICATION]` markers remain. This document
consolidates the design decisions and the grounding in existing code.

## Decision 1 — How a self-hosted target is declared

**Decision**: A separate **routing table keyed by `(backend, model)`** maps to a
target descriptor (base URL + strategy + normalizer keys + optional clean
reroute upstream), decoupled from per-backend config.

**Rationale**: Quirks are per response-format/model-family and shared across
agents (one harmony normalizer serves every tool-using agent on gpt-oss), so the
declaration must key on the *model the backend is pointed at*, not on the backend
alone. A standalone table keeps the surface legible and avoids threading
target-resolution logic through every backend's config block. A missing
`(backend, model)` entry deterministically means native-cloud → no-op.

**Alternatives considered**:
- *Per-backend config block* — rejected: duplicates normalizer declarations
  across backends sharing a model; couples robustness policy to backend identity.
- *Inference from the provider-override URL* — rejected: implicit/auto-detected
  behavior contradicts FR-078-2's "only explicitly-declared normalizers run."

## Decision 2 — Smoke-test failure behavior

**Decision**: **Auto-reroute-then-fail-closed.** On probe failure, auto-reroute
to the target's declared clean upstream if one exists; otherwise fail-closed
(block the card with a clear, specific error). Never fail-open.

**Rationale**: The 077 model-hang chase showed the cost of black-holing a card
mid-lifecycle. Failing fast at startup with a legible error is strictly better;
auto-reroute captures the openclaw→Ollama-direct fix as automatic recovery when a
clean path is already declared. Fail-open onto a known-broken path is exactly the
077 failure mode and is prohibited.

**Alternatives considered**:
- *Fail-open-with-warning* — rejected: reintroduces the silent-corruption /
  black-hole failure the feature exists to kill.
- *Always fail-closed (no auto-reroute)* — rejected: discards a known-good
  recovery path and forces manual intervention the table already describes.

## Decision 3 — Normalizer selection

**Decision**: **Explicit** — the operator lists normalizer keys per routing-table
entry; only declared normalizers run (no auto-detection).

**Rationale**: Deterministic, testable, and auditable. Auto-detection risks
applying a transform to a response that doesn't need it (corruption) and makes
the no-op guarantee (SC-003) harder to reason about.

**Alternatives considered**:
- *Auto-detect by sniffing response markers* — rejected: nondeterministic, and
  detection false-positives corrupt otherwise-valid responses.

## Grounding in existing code

- **073 `ClaudeCodeShim`** (`backends/claude_code_shim.py`): same-process aiohttp
  reverse proxy bound to `127.0.0.1:ephemeral`, strips `thinking` blocks from JSON
  + SSE; never logs auth token or bodies. This is the prototype; its reasoning-strip
  becomes `normalizers/reasoning.py` and its transport becomes the shared `shim.py`.
- **080 `DualModelProxy`** (`performer/proxy/`): generalized the reverse-proxy
  shell; `dual_model_proxy.py` (aiohttp server, `start()`→loopback base URL),
  `upstreams.py` (pure render/parse for `openai` + `anthropic` wire formats),
  `launch.py` (`maybe_launch_proxy(orchestration, backend_name, env)` +
  `PROVIDER_BASE_URL_ENV` map + env-restore lifecycle). 078 extends this seam.
- **Activation seam** (`main.py:~1107`): `maybe_launch_proxy(...)` is already
  invoked after `clone_repository`, before `backend.start()` — the natural hook
  for routing-table resolution + smoke-test gating, parallel to 080.
- **Env redirect + restore**: `PROVIDER_BASE_URL_ENV` maps backend→provider base
  URL env var (codex→`CODEX_PROVIDER_BASE_URL`, …, claude_code→`ANTHROPIC_BASE_URL`);
  `reroute` reuses exactly this to repoint at the clean upstream, and the
  `env_restores` lifecycle prevents cross-job leakage (see `test_launch.py`).
- **Backend with no env mapping** (hermes, in `UNSUPPORTED_BACKENDS`): `reroute`
  cannot redirect it → must surface a clear "cannot route" error (Edge Case +
  FR-078-4), mirroring 080's `ProxyLaunchError("no provider-base-URL override")`.

## Best practices applied

- **Stateful SSE filtering**: buffer across chunk boundaries; never emit a
  half-parsed tool call (harmony deltas can split mid-token). Reuse 073's SSE
  handling shape.
- **Timeout-bounded probe**: the smoke test uses a bounded `httpx` timeout so a
  wedged upstream surfaces as "unhealthy" rather than hanging startup; composes
  with (does not replace) the 077 stall watchdog.
- **Pure normalizers behind a registry**: format-keyed, side-effect-free
  transforms → trivially unit-testable and reusable across backends (SC-006).
- **Observability discipline**: INFO logs limited to method/path/status/latency +
  the normalizer/strategy/health decision; never the token or bodies (FR-078-10).
