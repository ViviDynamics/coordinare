# Quickstart: Validating Spec 122

How to verify each story. The US2 matrix uses real ephemeral containers against the live LiteLLM
gateway; code changes use `.venv/bin/pytest` + `.venv/bin/ruff`.

## Prereqs

- Docker healthy; `coordinare-performer:full`/`:extra` present.
- `.env` has `LITELLM_MASTER_KEY` (the gateway master key) + a usable GitHub token.
- LiteLLM serves `spark/gpt-oss:120b`/`:20b` (operator-owned; verify: a `/v1/chat/completions` probe
  with `model=spark/gpt-oss:120b` returns content — see research.md).

## US2 — Run the compatibility matrix (the gate)

1. Run the extended harness pointed at LiteLLM (throwaway containers, test config — does NOT touch the
   running daemon): `scripts/smoke_backends.py --via-litellm` (or `--config <litellm-test-config>`),
   optionally `--backends <id,...>`.
2. Confirm it emits one row per backend with `launched / completed / output_present /
   contract_satisfied / normalizers_needed / gateway_available / verdict`.
3. Spot-checks:
   - A backend known-good through LiteLLM → `verdict=compatible`.
   - Re-run with a candidate normalizer removed → if still `compatible`, that normalizer shows up as
     NOT in `normalizers_needed` (redundant).
   - Simulate gateway down (bad base-URL) → `verdict=gateway_unavailable`, not `incompatible`.
4. The run is idempotent: re-running on an unchanged system yields the same matrix (SC-007).

## US1 — Each backend reaches LiteLLM

- For each `compatible` backend, the matrix row proves it reached `spark/gpt-oss:120b` through LiteLLM
  (not `192.168.3.30:11434`). claude_code specifically must pass via the Anthropic `/v1/messages`
  front door with no orchestrator translate shim.
- Negative: a backend mis-pointed (wrong provider base-URL) shows a connection/"no such model" error,
  not a false pass.

## US3 — Migrate routing + retire shims

**Unit (code):**
- Any normalizer removed from the registry → its targeted unit test removed/updated; remaining
  normalizer + self-hosted-layer tests stay green (`.venv/bin/pytest`).
- `.venv/bin/ruff check` clean on changed files.

**Config (operational, validated by inspection + a live card):**
- After migration: grep `routing.yaml`/`config.yaml` → no migrated backend targets
  `192.168.3.30:11434` (SC-003); dead `spark/*` self-hosted + unused Ollama-direct `model_endpoints`
  are gone (no dangling `mode` refs); `config.example.*`/`routing.example.yaml` reflect the new topology.
- Every retained normalizer maps to a matrix row listing it in `normalizers_needed` (SC-004).

**End-to-end (SC-005):**
- Restart the daemon on the migrated config; drive one card through the full lifecycle; confirm each
  stage's backend reaches LiteLLM and the card completes without reverting to Ollama-direct.

## Gates / discipline

- Matrix recorded in the spec dir for traceability (FR-016).
- No secret value in any matrix row / log / notification — grep the artifact + new logs (SC-006).
- Adversarial review before merge (diverse-lens + refute-verify), per project discipline.
- A backend that fails the matrix is NOT migrated (stays on current routing) and is surfaced (FR-013).
