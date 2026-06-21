# Contract: Route the hermes (tech_writer) Backend Through the Self-Hosted Shim

Makes hermes eligible for the 078 normalize-mode shim (opt-in via routing table),
with the 099 completion probe + the 098 normalizer chain. Default (no hermes
entry) and all other backends are unchanged.

## Routed-vs-default behavior

| Condition | hermes provider base | Normalization | Health probe |
|---|---|---|---|
| No hermes routing entry (default) | hermes's configured `HERMES_BASE_URL` (direct) | none (as today) | none |
| hermes routing entry (normalize) | `<shim-loopback>/v1` (CLI appends `/chat/completions`) | `strip_control_chars` + `strip_reasoning` | completion |

## Invariants (MUST)

1. **Eligibility (FR-001):** hermes is no longer in `UNSUPPORTED_BACKENDS` and has a `PROVIDER_BASE_URL_ENV` mapping; a hermes routing entry resolves and launches the normalize shim.
2. **Normalize-before-parse (FR-002, SC-001):** the model response passes through the declared normalizers (control-char strip + reasoning promote) before hermes's strict JSON parser — a control-char/reasoning-wrapped body becomes clean parseable content.
3. **Completion gating (FR-003, SC-002):** the hermes target uses the completion probe; healthy on a non-empty normalized completion, fail-closed (or auto-reroute) on empty/non-200/timeout. Never fail-open.
4. **Correct wire path (FR-004, SC-003):** the loopback base is `<loopback>/v1` so hermes's appended `/chat/completions` hits a served route; a misaddressed base is caught at the startup probe, never a silent mid-job 404. junie's verbatim full-path suffix is unchanged.
5. **Default-safe / opt-in (FR-005, SC-004):** with no hermes entry, hermes and every other backend behave byte-for-byte as before.
6. **Secret-free + fail-closed-only-at-health (FR-006, SC-005):** decisions carry only mode/status/action/names; health gating stays the only fail-closed surface; normalizers fail-open.
7. **No new dependency / no state change (FR-007).**

## Reused observability record: `proxy.health`

```json
{
  "event": "proxy.health",
  "path": "http://127.0.0.1:PORT/v1/chat/completions",
  "status": "healthy | unhealthy",
  "resolved_action": "proceed | rerouted | fail_closed",
  "wire_format": "openai",
  "strategy": "normalize",
  "mode": "completion"
}
```
