# Spec 119: Malformed-output resilience for JSON-only roles (documenting phase)

## Problem (live: website #177 / issue #169, 2026-06-25)

The documenting phase (`tech_writer` role) runs on **hermes → gpt-oss:120b** through the
spec-100 normalize shim. Spec 100 fixed the *frequency* of malformed output (moved off
`spark/qwen3.6:35b`, added `strip_reasoning`/`strip_control_chars` + hermes's own
`_extract_json_object`), but gpt-oss:120b is a **stochastic reasoning model** and still
*occasionally* emits output with no parseable JSON object. When that happens hermes reports
`error_reason="malformed_output"`.

The retry path in `monitor_performer` (spec 098) routes a failure to `handle_system_error`
(backoff + bounded retry → re-dispatch) only when the reason starts with
`BACKEND_FORMAT_ERROR:` **or** matches `_is_transient_backend_error` (transport/CLI-crash
markers). hermes emits the **bare** string `malformed_output`, which matches **neither** — so
the documenting phase falls straight through to a **terminal block on the very first bad
parse**, with zero retries. Spec 098 gave the assessor this resilience; `tech_writer` was never
connected to it. Net: a single stochastic malformed roll permanently parks the card in BLOCKED
(observed: #177 blocked at `documenting`, `reason=malformed_output`).

## Requirements

1. Treat a backend `malformed_output` reason (a JSON-only role's output-format-contract
   failure) as a **retryable** error: route it through the existing `handle_system_error`
   bounded-retry path (re-dispatch, backoff, budget) instead of terminal-blocking — identical
   to how spec-098 assessor-shape / `BACKEND_FORMAT_ERROR:` failures are handled.
2. The persisted `system_error_reason` for a matched malformed_output MUST be tagged with the
   `BACKEND_FORMAT_ERROR:` prefix (idempotently), so the retry gate matches it on subsequent
   cycles and it stays re-classifiable at budget exhaustion — same discipline as the
   assessor-shape tagging.
3. The bounded retry budget (`handle_system_error`: N consecutive fails) is unchanged: a
   genuinely deterministic malformed_output (e.g. doc truncated at `max_tokens`) exhausts the
   budget and blocks — the correct floor. Stochastic malformation resolves on a re-dispatch.
4. No false widening: only `malformed_output` (and close kin) routes through this; real content
   verdicts and normal terminal errors are unaffected.
5. **Diagnosability:** enable `performer_log_dir` in the deployment config so hermes persists
   per-job `stdout`/`stderr`/`prompt` artifacts (bind-mounted to the host) — turning future
   malformed cases from guesswork into ground truth (and revealing if `max_tokens` truncation
   ever contributes, which a retry would not fix). This is a config change (gitignored
   `config.yaml`), not code.

## Out of scope

- Changing the documenting output contract, `max_tokens`, or the normalizer chain — revisit
  only if captured artifacts show a deterministic cause (truncation / a new unsalvageable
  shape).
- The runner / env / classification work (114–118), unrelated.

## Acceptance

- A performer error with `reason=malformed_output` at a JSON-only stage (e.g. `documenting`)
  → `phase=system_error`, `system_error_count` incremented, stage NOT advanced/abandoned, and
  `system_error_reason` carries the `BACKEND_FORMAT_ERROR:` prefix — i.e. it RETRIES, it does
  not terminal-block on the first occurrence.
- Transient-crash and normal terminal-error behavior unchanged. Coordinare unit suite green;
  adversarial review before merge.
- Deployed: `performer_log_dir` set so the next malformed case is captured on the host.
