# Contract: Per-Backend LiteLLM Compatibility Matrix

The artifact that gates migration (US2). Produced by the extended `scripts/smoke_backends.py` run
against LiteLLM `spark/gpt-oss:120b`. This registers the fields downstream steps (US3 migration
decisions) depend on, so the matrix shape can't silently drift.

## Field Registry

| Field | Type | Required | Producer | Consumer |
|---|---|---|---|---|
| `backend` | str | yes | harness | US3 (which to migrate) |
| `endpoint_id` | str | yes | harness | traceability |
| `model` | str | yes | harness | confirms `spark/gpt-oss:120b`/`:20b` |
| `launched` | bool | yes | harness | verdict |
| `completed` | bool | yes | harness | verdict |
| `output_present` | bool | yes | harness | verdict |
| `contract_satisfied` | bool | yes | harness | verdict |
| `normalizers_needed` | list[str] | yes | harness (re-run minus normalizer) | US3 keep/drop shims (FR-010) |
| `gateway_available` | bool | yes | harness | distinguishes availability failure (FR-007) |
| `verdict` | str enum | yes | harness | US3 migrate-only-if-`compatible` (FR-009/013) |
| `note` | str | optional | harness | names/counts/reasons only — NO secret values (FR-014) |

## Verdict rules

- `verdict == "compatible"` ⟺ `launched ∧ completed ∧ contract_satisfied` (FR-006).
- `verdict == "gateway_unavailable"` when `gateway_available == false` (gateway 5xx / model not served)
  — NOT counted as a backend incompatibility (FR-007).
- `verdict == "incompatible"` otherwise (launched but failed to complete / empty output / contract miss).
- `normalizers_needed == []` ⟹ all orchestrator normalizers are redundant for this backend through
  LiteLLM (drop them in migration). Non-empty ⟹ keep exactly those for this backend (FR-010).

## Invariants

- The matrix is reproducible: same system → same matrix (SC-007).
- Generated against **throwaway** containers + a test `--config`; never mutates the live fleet (FR-008).
- No field carries a secret value; the LiteLLM master key never appears (FR-014).
- A backend is migrated in US3 **only** if its row is `compatible`; an `incompatible` backend stays on
  its current working routing and is surfaced (FR-013).

## Decision table (US3 action per matrix row)

| verdict | normalizers_needed | US3 action |
|---|---|---|
| compatible | `[]` | migrate to LiteLLM; **remove** routing shim/normalizers for this backend |
| compatible | `[X]` | migrate to LiteLLM; **keep** normalizer(s) X (re-point their routing entry's base_url to LiteLLM) |
| incompatible | — | **do not migrate**; keep current routing; surface for follow-up |
| gateway_unavailable | — | **re-run** the matrix (transient); do not act on this row |
