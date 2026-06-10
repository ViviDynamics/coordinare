# Phase 0 Research: Anthropic→OpenAI Request-Translating Shim

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08

The spec checklist carries **0 `[NEEDS CLARIFICATION]`** markers and the requirements checklist is
fully `[x]`, so Phase 0 resolves no open unknowns. Instead it records the binding design decisions
that the implementation plan rests on, each grounded in the existing 078/073 code already read.

---

## Decision 1: Surface translation as a new `translate` strategy value (not a new shim mode flag)

- **Decision**: Extend `Strategy = Literal["normalize", "reroute"]` in `proxy/routing.py` to
  `Literal["normalize", "reroute", "translate"]`. A `(claude_code, model)` entry selects translation
  by `strategy: translate`.
- **Rationale**: The routing model already dispatches on `strategy` in `launch.py._launch_for_target`,
  and `TargetDescriptor._validate_strategy` already encodes per-strategy invariants. Adding a third
  enum value reuses that dispatch and validation seam with the least surface area, and keeps the
  operator-facing schema consistent with the documented 078 contract (one `strategy:` key per entry).
  FR-006 explicitly asks for "a per-entry choice ... leaving reroute/normalize intact."
- **Alternatives considered**:
  - *A boolean `translate: true` flag orthogonal to strategy* — rejected: creates a 2×3 combinatorial
    space (translate × {normalize,reroute,—}) most of which is meaningless, and multiplies validation
    branches. The strategy enum already models "what this entry does" as one mutually-exclusive choice.
  - *A separate routing table / separate env var for claude_code* — rejected: fractures the single
    `(backend, model)` resolution path and the fail-closed loader; violates the additive
    single-table model FR-007 depends on.

## Decision 2: `translate` always launches a loopback shim (like `normalize`), never bare reroute

- **Decision**: `strategy: translate` always launches the `SelfHostedShim` on `127.0.0.1:0` and points
  `claude_code`'s `ANTHROPIC_BASE_URL` at it (via `PROVIDER_BASE_URL_ENV["claude_code"]`). It is never a
  bare env-repoint like `reroute`.
- **Rationale**: `reroute` works only when the backend already speaks the upstream's wire protocol — it
  does no in-process work. Translation is inherently in-process request+response rewriting, so it needs
  the shim process. This mirrors `normalize`, which also launches the shim; `translate` is "normalize
  plus a request-side and a wire-format response-side transform." Reusing the shim also inherits its
  non-200 passthrough (FR-012), hop-by-hop header handling, and capture-dir observability.
- **Alternatives considered**: *Translate inside a reroute-style env repoint* — impossible; there is no
  process to translate in.

## Decision 3: Translators are pure functions in a new `translate/` sub-package

- **Decision**: Put the request translator (`translate/request.py`), non-streaming response translator
  (`translate/response.py`), and streaming SSE translator (`translate/sse.py`) in a new package, all
  pure (no aiohttp / network). `sse.py` subclasses the existing `StatefulSSEFilter` so it slots into the
  `_FilterChain` already used for normalizers.
- **Rationale**: Constitution I (single responsibility) and II (deterministic, isolated unit tests).
  Pure translators can be tested against captured/stub bodies and synthetic SSE frame sequences with no
  network, satisfying SC-005 and the per-FR independent tests. The `StatefulSSEFilter` base already
  solves chunk-boundary buffering (split on `\n\n`), which FR-003 requires.
- **Alternatives considered**: *Inline translation logic directly in `shim.py` handlers* — rejected:
  couples wire-format knowledge to the transport layer, makes the transform hard to unit-test in
  isolation, and bloats a file whose job is proxying.

## Decision 4: Translate composes with normalizers — translate is the OUTERMOST response transform

- **Decision**: On the response path the order is **upstream OpenAI bytes → normalizers
  (`harmony_tool_calls`, `strip_reasoning`) → wire-format translation (OpenAI→Anthropic) → CLI**. The
  normalizers run first (on OpenAI-shape JSON/SSE, which is exactly the shape they were written for in
  073/078), then the OpenAI→Anthropic translator runs on the already-normalized OpenAI structure.
- **Rationale**: `HarmonyToolCallsNormalizer` and `StripReasoningNormalizer` operate on OpenAI-wire
  fields (`tool_calls`, `reasoning_content`, OpenAI SSE deltas). They must see OpenAI shape, so they
  belong upstream of the wire translation. After harmony reassembly produces clean structured OpenAI
  `tool_calls`, the translator maps those to Anthropic `tool_use` blocks (FR-004). `strip_reasoning`
  also has an Anthropic-side path (`thinking`/`redacted_thinking`); on the translate path it runs on the
  OpenAI `reasoning_content` side, and the translator simply does not emit Anthropic `thinking` blocks —
  no double handling.
- **Alternatives considered**: *Translate first, then normalize on Anthropic shape* — rejected: the
  existing normalizers do not understand Anthropic `tool_use`/`thinking` blocks for the harmony-leak case
  (the leak is an OpenAI-shape phenomenon); re-targeting them would duplicate the harmony logic.

## Decision 5: Field mapping defaults are deterministic and documented (no silent drops)

- **Decision**: Every Anthropic↔OpenAI field with no clean counterpart gets a documented deterministic
  mapping/default in `contracts/request-translation.md` and `contracts/response-translation.md`
  (e.g. Anthropic `system` string/array → OpenAI leading `system` message; Anthropic `stop_sequences` →
  OpenAI `stop`; `max_tokens` required by Anthropic → OpenAI `max_tokens`; `tool_choice` shapes;
  `finish_reason` ↔ `stop_reason` table per FR-005). Anything intentionally dropped is recorded in the
  contract, satisfying the edge case "rather than silently dropping data without record."
- **Rationale**: FR-001/FR-002/FR-005 and the "field with no clean equivalent" edge case demand
  determinism and traceability. A contract table is the testable artifact (`test_translate_request.py`,
  `test_translate_response.py` assert against it).
- **Alternatives considered**: *Best-effort passthrough of unknown fields* — rejected: risks leaking
  Anthropic-only fields into an OpenAI request the upstream rejects, and is non-deterministic.

## Decision 6: `finish_reason` ↔ `stop_reason` mapping table

- **Decision**: Canonical map — OpenAI `stop` → Anthropic `end_turn`; `length` → `max_tokens`;
  `tool_calls` → `tool_use`; `content_filter` → `end_turn` (closest safe terminal); unknown/absent →
  `end_turn`. Applied identically in non-streaming (`response.py`) and streaming (`sse.py` `message_delta`).
- **Rationale**: FR-005 mandates the mapping in both modes; the claude CLI only accepts the Anthropic
  vocabulary. Centralizing the table in one helper keeps streaming and non-streaming consistent.
- **Alternatives considered**: *Per-mode ad-hoc mapping* — rejected: drift risk between the two code paths.

## Decision 7: Health gate exercises the full translate round trip (FR-010)

- **Decision**: Extend `health.py` so a `translate` target's probe sends a representative Anthropic
  `/v1/messages` body **through the request translator**, forwards the translated OpenAI request to the
  upstream, runs the response back **through the response translator + normalizers**, and asserts a
  structured Anthropic `tool_use` block (or valid terminal response) comes out — not mere upstream
  reachability. Reuses the existing `gate()` state machine: healthy→proceed, unhealthy+`reroute_upstream`
  →rerouted, unhealthy+none→fail_closed. MUST NOT fail open.
- **Rationale**: FR-010 is explicit that reachability alone is insufficient — the translation itself is
  what's under test. The existing `check_health` already branches on `wire_format` for probe shape;
  the translate branch adds the round-trip assertion. Fail-closed is already the `gate()` default for
  unhealthy without `reroute_upstream`.
- **Alternatives considered**: *Reuse the OpenAI-wire reachability probe unchanged* — rejected: would
  pass a reachable-but-untranslatable upstream and break the card mid-job, the exact failure FR-010 bans.

## Decision 8: Observability stays at metadata level (FR-011 / FR-078-10)

- **Decision**: Translate-path logs emit only method/path/status/latency + which translator ran + which
  normalizers ran. No request body, no response body, no auth token, no `auth_env` *value* (only the env
  var name may appear, per the standing security rule).
- **Rationale**: Hard security invariant repeated in FR-011, FR-078-10, and the project memory. The 073
  `ClaudeCodeShim` and 078 `SelfHostedShim` already model exactly this INFO-log shape; the translate path
  conforms to it. SC-004 is the test assertion.
- **Alternatives considered**: *Debug-level body logging behind a flag* — rejected: even flag-gated body
  logging violates the stated invariant and risks accidental enablement in prod.

---

## Open follow-ons (explicitly out of scope for this spec)

- Flipping the live `config.yaml` `qa` assignment to the translate path — a live-verified follow-on once
  the capability ships and a real qa run confirms parity-or-better.
- Any re-architecture of the 073 `ClaudeCodeShim` or the LiteLLM deployment.
- New backends/models beyond enabling `claude_code` over the existing self-hosted Ollama gpt-oss target.
