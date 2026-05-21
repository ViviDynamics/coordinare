# Phase 0 — Research: Compatibility-First Performer Backend

All five Open Questions from `spec.md` are resolved below. No `NEEDS CLARIFICATION` tags remain.

---

## R1. Harness choice — opencode vs junie-style one-shot vs in-tree

**Decision**: Use **opencode** as the harness, wrapped in a brand-new adapter `opencode_compat.py` that mirrors (but does **not** subclass) `OpenCodeAdapter`.

**Rationale**:
- opencode already runs all tools agent-side by design (`read_file`, `grep_repo`, `list_dir`, `which`, etc. are all in-process). The "no hosted tools" requirement (FR-003) is satisfied by configuration, not by code we have to write.
- The opencode CLI is already vendored in the performer image — no new container payload, no new runtime dependency.
- opencode's `serve` HTTP API speaks straight `/v1/chat/completions` against any `base_url`, which is exactly the LCD profile FR-002 demands.
- A junie-style one-shot CLI would force us to re-implement tool execution from scratch — a large new surface.
- A purpose-built in-tree harness has the smallest dependency footprint but the largest implementation cost and ongoing maintenance burden.

**Alternatives considered**:
- **junie-style one-shot CLI** — rejected: re-implements opencode's existing agent-side tool loop.
- **In-tree harness** — rejected: scope creep; we'd be re-creating opencode's session/event model.
- **Subclass `OpenCodeAdapter`** — rejected per [[feedback_junie_own_harness]]: each harness gets its own adapter even when the underlying CLI is similar; tomorrow's opencode version may drift in ways that turn shared inheritance into a refactor liability.

**Spike confirmation**: A one-hour spike against opencode's tool-call protocol (running `opencode serve --no-hosted-tools` and inspecting the request body emitted to a local httpbin echo server) verified the LCD profile is achievable with config alone; no patches to opencode needed.

---

## R2. Backend value name

**Decision**: `opencode_compat`.

**Rationale**:
- Names the harness honestly (per R1) so the operator can map the config value to the binary they're running.
- The `_compat` suffix flags the LCD-profile distinction from the existing `opencode` value, which keeps hosted-tool descriptors enabled.
- Avoids the trap of a "neutral" name like `lcd` or `portable` that hides the actual binary in play.

**Alternatives considered**:
- `compat` — rejected: too generic; doesn't tell the operator what's actually running.
- `lcd` — rejected: jargon; bad UX.
- `opencode` (overload the existing key with a flag) — rejected: violates SC-003's swap test (changing backend should be one line, not two).

**Migration note**: existing `opencode` key keeps its current behaviour for back-compat. Both keys appear in `supported_backends` in `agent/performer/src/performer/backends/__init__.py`.

---

## R3. Agent-side web_search

**Decision**: Defer the implementation; require only that any web_search invocation routes through an opencode function-call handler, not a `type: web_search` tool descriptor.

**Rationale**:
- The 067 motivating cards (env_bootstrap, assessor, architect, implementer, reviewer, qa, closer on a website card) do not require web_search to complete — the env_bootstrap loop and downstream graph paths use repository-local tools.
- Picking a specific search provider (Brave / SearXNG / Tavily) is a separate spec because it has its own cost/key/self-host tradeoffs.
- The constraint we MUST land in 067 is the **shape**: any future web_search lives behind a function-call handler the performer owns. Codifying this now prevents a future spec from accidentally re-introducing a hosted descriptor.

**Implementation in 067**: `packages/service_inference/src/coordinare_service_inference/prompt.py` is audited to ensure no prompt mentions web_search as a tool the model should expect; any such mention is removed or qualified with "agent-side only, request via function call."

**Alternatives considered**:
- Ship a Brave Search agent-side handler in 067 — rejected: scope creep; not required by any P1 acceptance scenario.

---

## R4. `developer` role policy

**Decision**: **Remap to `system` with a one-time WARN log per session**, gated on `compat_remap_developer_role: true` (default true).

**Rationale**:
- Strict refusal (purer) would block any prompt template that uses `developer` from running on the compat backend without manual rewrites — a high-friction failure mode for operators trying the LCD path for the first time.
- A silent remap (friendlier) would let `developer`-coded prompts drift on the compat backend without anyone noticing the divergence from codex+OpenAI semantics.
- The one-time WARN gives the operator exactly one signal per session: "your prompt uses a role we had to downgrade." The signal is loud enough to fix at leisure but quiet enough not to flood logs.
- The flag is a safety net: if a future LM Studio (or similar) ever does accept `developer`, the operator can flip the flag and let the role pass through unchanged.

**Alternatives considered**:
- **Refuse outright** — rejected: violates the spirit of FR-001 (compat as the *portable* path).
- **Silent remap** — rejected: undetectable divergence between codex and compat is exactly the failure mode 065 hit.

---

## R5. Debug-log redaction of full request bodies (NFR-002)

**Decision**: Add a `_redact_request_body()` helper in `opencode_compat.py` that walks the dict and replaces values whose **keys** match a denylist (`api_key`, `authorization`, `OPENAI_API_KEY`, `token`, `secret`, `password`, `bearer`) with `"***REDACTED***"`. For prompt content (where secrets are embedded mid-string, e.g. echoed `.env.example` contents), apply a pattern-based pass for `OPENAI_API_KEY=...`, `sk-...`, `gho_...`, `ghp_...`, `Bearer ...` substrings.

**Rationale**:
- NFR-002 requires debug-level inspectability of the request body. Operators debugging env_bootstrap failures need to see what was actually sent.
- The 065 incident proves that prompt content can include secrets (env_bootstrap reads `.env.example` and may echo lines back in tool calls).
- Key-based redaction handles the outer envelope (API key in headers). Pattern-based redaction handles secrets inside prompt text. Both are cheap (single-pass regex / dict walk).
- Helper lives in the adapter, not the coordinare, so the coordinare log surface remains unchanged.

**Alternatives considered**:
- **No redaction, "trust the operator"** — rejected: secrets-in-logs is a recurring incident pattern; operator self-discipline doesn't survive a 2am page.
- **Encrypt logs at rest** — rejected: out of scope; doesn't solve the in-memory log-line case.

**Test coverage**: `tests/unit/test_067_compat_request_shape.py` includes a redaction sub-suite asserting that representative secret patterns are scrubbed.

---

## R6. Endpoint coverage in CI

**Decision**: LM Studio is the only endpoint exercised in CI (via T021, gated by `LMSTUDIO_AVAILABLE`). vLLM, Ollama, and LiteLLM proxy are covered by an operator hand-run of the swap test (T031), with results recorded in `swap-test-results.md`.

**Rationale**:
- The LCD outbound validator (`_assert_lcd_payload`) is the structural guarantee that the payload is portable across any OpenAI-compatible `/v1/chat/completions` endpoint. Per-endpoint integration tests would re-test the same wire-shape contract at roughly 4× the infrastructure cost (four serve runtimes, four model loads, four `<ENDPOINT>_AVAILABLE` gating envs).
- The hand-run record gives traceable evidence that the swap test (SC-003) passes on each endpoint without paying CI cost for unchanging behaviour.
- FR-006 is narrowed accordingly — see `spec.md`.

**Alternatives considered**:
- **Full matrix parameterization in CI** — rejected: cost outweighs marginal signal beyond the structural LCD validator.

---

## Post-Phase-1 Constitution re-check

After drafting `data-model.md` and `contracts/upstream_http_error.md`:

| Principle | Re-check | Notes |
|-----------|----------|-------|
| I. Code Quality First | ✅ | No new abstractions invented; adapter-per-harness rule honored. |
| II. Testing Discipline | ✅ | Five new test files cover unit, integration, contract, and perf budgets. |
| III. UX Consistency | ✅ | Verbatim upstream-body logging is strictly better operator UX than the current wrapper. |
| IV. Performance by Design | ✅ | NFR-001 has a defined benchmark and a CI-skippable runner. |
| V. Clarity Before Action | ✅ | All Open Questions resolved above. |

No violations. No `NEEDS CLARIFICATION` tags remain in the feature artifacts.
