# Data Model: Spec 122

No persisted coordinare state and no schema migration. The entities below are (a) the **generated
compatibility-matrix artifact** and (b) the **config surfaces** being migrated.

## Compatibility Matrix (generated artifact — US2)

One row per backend, produced by the extended `smoke_backends.py` run against LiteLLM
`spark/gpt-oss:120b`. Written to `tmp/` and recorded in this spec dir for traceability (FR-016).

| Field | Type | Meaning |
|---|---|---|
| `backend` | str | claude_code / codex / opencode / junie / pi / openclaw / hermes |
| `endpoint_id` | str | the test endpoint id used |
| `model` | str | `spark/gpt-oss:120b` (or `:20b`) |
| `launched` | bool | container came up + CLI installed |
| `completed` | bool | job reached terminal success |
| `output_present` | bool | non-empty backend output |
| `contract_satisfied` | bool | role contract met (e.g. parseable QA verdict) |
| `normalizers_needed` | list[str] | normalizers the backend STILL needs through LiteLLM (empty ⇒ all redundant) |
| `gateway_available` | bool | gateway reachable + model served (false ⇒ availability failure, not a backend defect) |
| `verdict` | enum | `compatible` (launched∧completed∧contract_satisfied) / `incompatible` / `gateway_unavailable` |
| `note` | str | names/counts/reasons only — no secret values |

**Rules:**
- `verdict = compatible` ⟺ `launched ∧ completed ∧ contract_satisfied` (FR-006).
- `gateway_unavailable` is distinct from `incompatible` (FR-007) — a transient gateway issue does not
  condemn a backend.
- `normalizers_needed` is determined by re-running with the candidate normalizer removed (FR-005): if
  still `compatible` → that normalizer is redundant for this backend.
- Only `compatible` backends are migrated (FR-002/FR-009/FR-013).

## Routing config (migration target — US3)

Not persisted state; these are the spec-080 catalogs + the self-hosted routing table.

| Entity | Where | Change |
|---|---|---|
| `model_endpoints[]` | config.yaml (live) + config.example.* | self-hosted ones → LiteLLM `spark/gpt-oss:120b`/`:20b`; delete dead `spark/*` + unused Ollama-direct |
| `modes[]` | config.yaml | reference the migrated model_endpoints |
| `endpoints[]` | config.yaml | the LiteLLM endpoint (base_url + auth via env) |
| routing-table entries | routing.yaml (live) + routing.example.yaml | re-point `base_url` Ollama→LiteLLM or remove entry; keep only matrix-proven normalizers |
| per-backend provider base-URL env | endpoint `env` blocks | point each backend's `*_PROVIDER_BASE_URL` / `ANTHROPIC_BASE_URL` / `HERMES_BASE_URL` at LiteLLM |

**Validation rules:**
- After migration, no `model_endpoints`/routing entry targets `192.168.3.30:11434` for a migrated
  backend (SC-003).
- Every routing entry that retains a normalizer has a matching matrix row with that normalizer in
  `normalizers_needed` (SC-004).
- Every deleted `model_endpoints` entry is referenced by no `mode` post-migration (no dangling refs).
- The committed `config.example.*` / `routing.example.yaml` reflect the migrated topology (kept in sync).

## Normalizer registry (code — US3)

`proxy/normalizers/` entries (`harmony_tool_calls`, `strip_reasoning`, `strip_control_chars`, …). A
normalizer is removed from the registry only if `normalizers_needed` is empty for it across ALL
backends; otherwise it stays registered and is merely dropped from migrated routing entries. Existing
normalizer/self-hosted-layer unit tests are the regression guard.
