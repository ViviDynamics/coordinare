# Research: Junie Assessor Resilience

## D1 — US1: how to make a junie parse/empty failure retryable

**Decision**: Tag the failure as a backend-format / transient error at the **reason source** (the junie backend / `http_performer_service` reason extraction), so it routes through `monitor_performer`'s **existing** retryable branch (`monitor_performer.py:3771`):
```
if reason.startswith(_FORMAT_ERROR_PREFIX) or _is_transient_backend_error(reason):
    # → system_error_count++, phase="system_error" → handle_system_error backoff +
    #   budget; blocks only after N consecutive fails.
```
Today junie's reason (`Junie failed with the message: Failed to build 'issue.md.junie_standalone'`) matches neither, so it hits the **default block** (`:3836`). Tagging it `BACKEND_FORMAT_ERROR:` (parse/format failure) or extending `_is_transient_backend_error` to match the junie parse-failure + empty-response shapes routes it to the existing bounded retry.

**Rationale**: reuses the entire system-error retry/backoff/budget machinery (no new counter, no new state, no new node). The MVP is a one-line classification change. Tagging at the source keeps the classifier honest (it knows it's a format/parse failure, not e.g. a model-capability prose failure which should NOT retry forever).

**Alternatives rejected**:
- *A new per-card assessor counter* — duplicates `system_error_count`; the system-error path already caps + blocks after N. Reuse it.
- *Match only in `_is_transient_backend_error`* — works, but matching free-text junie messages there is brittle; better to tag at the source where the backend knows the failure class. (We may do both: tag at source + a defensive matcher.)

## D2 — Distinguish empty-body (overload) from malformed (parse) — US3

**Decision**: At the reason source, classify three shapes:
- **malformed-body / empty-answer / truncated** → `BACKEND_FORMAT_ERROR:` (retryable; usually clears on retry/normalization).
- **empty-body / timeout** (no response at all) → a distinct transient marker; after the system-error budget exhausts on empty-body, classify the block as **ENV_BLOCKED** (spec-095: model unavailable/overloaded — not the card's fault).

**Rationale**: matches the reproduction (empty-body is the overload signal; malformed/empty-answer is per-response flakiness). ENV_BLOCKED already exists (095) for "infra, not the card."

**Alternatives rejected**: treating all failures identically — loses the operator-actionable "model is down" signal (US3/SC-003).

## D3 — US2: normalize junie's upstream via the shim

**Decision**: Launch a `normalize`-mode `SelfHostedShim` (via the existing `proxy/launch.py` `_launch_for_target`) in the junie performer, fronting the Ollama upstream, and point `JUNIE_PROVIDER_BASE_URL` at the shim's loopback. Add a **control-char-stripping normalizer** to `proxy/normalizers/` and include it (plus the existing `strip_reasoning` / #130 reasoning-promote and 082 envelope-completion) in the junie chain.

**Rationale**: the shim launcher already redirects other backends through normalizers; junie is the one backend still talking Ollama-direct (bypassing all protection). Two of the three failure shapes (control-chars, empty-but-reasoned answer) are exactly what normalizers fix → most flaky responses become clean, so US1 retries rarely fire.

**Alternatives rejected**: a junie-specific inline sanitizer — duplicates the shim's normalizer chain; reuse it.

## D4 — Control-char-stripping normalizer

**Decision**: A new normalizer that removes invalid/unescaped control bytes (`< 0x20` except `\t\n\r`, which JSON allows when escaped) from response string fields before the strict client parses, on both JSON and SSE paths (mirroring `strip_reasoning`'s shape). Fail-open: a clean body passes through unchanged.

**Rationale**: the reproduction showed raw control bytes in the response body breaking JSON parsing; stripping them yields a parseable body. Generic + reusable by any strict client (not just junie).

## D5 — Bounding & no-thrash

**Decision**: Reuse the system-error budget (`handle_system_error`) cap — after N consecutive assessor failures the card blocks once (US3: as ENV_BLOCKED if empty-body), not in a tight loop. A clean response resets the counter (existing behavior).

**Rationale**: no new bounding logic; the system-error path already caps + resets.

## D6 — Observability

**Decision**: Emit a secret-free `assessor.parse_failure` record with the failure **shape** (`empty_answer` / `malformed_body` / `empty_body` / `truncated`) + attempt number — no raw model output, no tokens. Keep the existing system-error/terminal logs.

**Rationale**: turns the opaque "Failed to build issue.md" into an actionable shape signal (SC-004) without leaking content.
