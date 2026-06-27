# Implementation Plan: Consolidate All Agent Backends on the LiteLLM Model Gateway

**Branch**: `122-litellm-backend-routing` | **Date**: 2026-06-26 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/122-litellm-backend-routing/spec.md`

## Summary

Consolidate the whole backend fleet onto the LiteLLM gateway and prove it backend-by-backend before
migrating. Concretely: (US2) extend the existing `scripts/smoke_backends.py` harness into a
**LiteLLM compatibility matrix** that runs each backend against `spark/gpt-oss:120b` via LiteLLM and
records launched/completed/output/contract-satisfied + which normalizers are still needed; (US1) the
levers to point each backend at LiteLLM already exist (`PROVIDER_BASE_URL_ENV` + the spec-080
`model_endpoints`/`modes` catalogs + LiteLLM's `/v1/messages` and `/v1/chat/completions` front doors —
all verified live this session); (US3) re-point `routing.yaml` + `model_endpoints` from Ollama-direct
(`192.168.3.30:11434`) to LiteLLM, drop the normalizer/strategy shims the matrix proves redundant,
delete dead `spark/*` self-hosted + unused Ollama-direct entries, and verify a full card on
gateway-only routing.

The work is **config-centric** (the live `config.yaml`/`routing.yaml` are gitignored deployment
state) plus a **committed harness/example/code surface**. The LiteLLM *server* config is out of scope
(operator-owned; `spark/gpt-oss:120b`/`:20b` already served — verified).

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — the spec-080 catalogs (`endpoints` / `model_endpoints` /
`modes` in config), `proxy/launch.py` (`maybe_launch_proxy`, `PROVIDER_BASE_URL_ENV`),
`proxy/routing.py` (`RoutingTable` + `TargetDescriptor`), `proxy/normalizers/` (harmony_tool_calls,
strip_reasoning, strip_control_chars), the backends (`agent/performer/src/performer/backends/`),
`scripts/smoke_backends.py` (the per-backend QA-smoke harness to extend), httpx. **No new dependency.**
**Storage**: N/A. No persisted coordinare state. The migration target is config: the live
`config.yaml` + `routing.yaml` (gitignored deployment state, edited operationally) and the committed
`config.example.*.yaml` / `routing.example.yaml`. The compatibility matrix is a generated artifact
(written under the repo's `tmp/` + recorded in the spec dir for traceability).
**Testing**: `scripts/smoke_backends.py` (the US2 matrix — real containers, the empirical proof) +
`.venv/bin/pytest` for any code changes (normalizer-registry / shim-wiring edits) + `.venv/bin/ruff`.
**Target Platform**: the coordinare daemon + ephemeral performer containers (`coordinare-performer:*`)
reaching `https://litellm.vividynamics.com`.
**Project Type**: single repo (coordinare daemon + performer package + scripts).
**Performance Goals**: no regression to dispatch/turn latency; consolidating on LiteLLM removes the
in-container shim launch + per-command normalization for migrated backends (net simplification). gpt-oss
via LiteLLM is the same upstream model, so model latency is unchanged.
**Constraints**: secret invariant (gateway master key only via the redacted secret channel;
names/counts/reasons in logs, never values); only humans approve PRs; the matrix must be reproducible
and must not touch the live running fleet (throwaway containers, separate `--config`).
**Scale/Scope**: 7 backends (claude_code, codex, opencode/opencode_compat, junie, pi, openclaw,
hermes); ~10 `model_endpoints`; 5 `routing.yaml` entries; affects all symphonies that use self-hosted
models (validated against the website symphony).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — ✅ Net *removal* of complexity (redundant shims/normalizers, dead
  model_endpoints). Harness extension is single-purpose (the matrix). No dead code left behind.
- **II. Testing Discipline (NON-NEGOTIABLE)** — ✅ The US2 compatibility matrix IS the empirical test
  gating migration (FR-004..008); any code change (normalizer-registry edits, shim-wiring removal)
  gets unit tests; the existing self-hosted-layer/normalizer unit tests must stay green (regression
  guard). Coverage must not regress.
- **III. UX Consistency** — ✅ Operator-facing surface: the matrix is a clear pass/fail table; the
  example configs document the new single-gateway topology. No behavior change a card-author sees,
  except the fleet becomes more reliable.
- **IV. Performance by Design** — ✅ Budget: migration removes per-dispatch shim launches + per-command
  normalization for migrated backends. SC-005 (a full card completes on gateway-only routing) is the
  perf/functional gate. No tracked path degraded.
- **V. Clarity Before Action** — ✅ Feasibility verified live (research.md records the probes); scope
  bounded (gateway server config out; frontier roles untouched; failing backends not migrated). No
  `NEEDS CLARIFICATION` remain — the two big forks (target catalog = `spark/gpt-oss`; normalization =
  verify-empirically) were answered by the operator.

**Result: PASS.** No violations; Complexity Tracking table not required (the feature net-reduces moving
parts).

## Project Structure

### Documentation (this feature)

```text
specs/122-litellm-backend-routing/
├── plan.md              # This file
├── research.md          # Phase 0 — verified probes + per-backend migration analysis + decisions
├── data-model.md        # Phase 1 — matrix + routing-config entities (no persisted state)
├── contracts/
│   └── compatibility-matrix.md   # the per-backend matrix row schema + pass/fail rules
├── quickstart.md        # how to run the matrix + verify the migration
├── checklists/
│   └── requirements.md  # spec quality checklist (passing)
└── tasks.md             # Phase 2 (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
# US2 — compatibility matrix (the committed proof harness)
scripts/smoke_backends.py            # extend: --via-litellm mode (point each backend at LiteLLM
                                     #   spark/gpt-oss:120b; emit the matrix incl. normalizer-needed)
scripts/smoke_backends_litellm.*     # (if a separate harness/config variant is cleaner than a flag)

# US1/US3 — pointing backends at LiteLLM + retiring shims (config + code)
config.example.yaml                  # documented single-gateway model_endpoints/modes + per-backend
config.example.*.yaml                #   provider-base-URL examples (committed surface)
routing.example.yaml                 # trimmed example routing (only empirically-needed normalizers)
agent/performer/src/performer/proxy/launch.py        # if a shim/strategy becomes dead, remove its wiring
agent/performer/src/performer/proxy/normalizers/     # remove a normalizer ONLY if no backend needs it
# Live (gitignored, operational — edited, not committed):
config.yaml                          # model_endpoints/modes → LiteLLM spark/gpt-oss; per-backend env
routing.yaml                         # re-point/trim Ollama-direct targets → LiteLLM (or remove)

# Tests (for any code change)
tests/unit/...                       # normalizer-registry / launch-wiring edits stay covered
agent/performer/tests/...            # self-hosted-layer tests stay green after shim removal
```

**Structure Decision**: Single repo, existing module boundaries. The *behavioral* migration is config
(`config.yaml`/`routing.yaml`, gitignored — edited operationally and validated by the matrix + a live
card). The *committed* deliverables are: the extended `smoke_backends.py` matrix, the updated example
configs documenting the single-gateway topology, and any code that removes now-dead shim/normalizer
wiring (with its tests). No new modules.

## Phasing & dependencies

- **US2 (matrix) is the gate and goes first** — it produces the per-backend pass/fail + normalizer-needed
  data that US3 acts on. It is independently runnable today (LiteLLM already serves the model).
- **US1 (reachability)** is mostly already-present machinery (`PROVIDER_BASE_URL_ENV` + catalogs +
  LiteLLM front doors); the matrix exercises it. Any gap the matrix finds (e.g. a backend that needs a
  config tweak to talk to LiteLLM) is fixed here.
- **US3 (migrate + retire)** depends on US2's matrix; it edits the live config + removes proven-redundant
  shims, then validates a full card.
- Recommended order: **US2 → US1 fixes (if any) → US3**.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| A backend regresses on LiteLLM (e.g. needs a normalizer the gateway doesn't replicate) | The matrix (US2) catches it BEFORE migration; that backend keeps its normalizer (FR-010) or stays on current routing (FR-013). |
| Removing a normalizer breaks a non-migrated path that still uses it | Remove a normalizer from the *registry* only if NO backend needs it; otherwise just drop it from the migrated routing entry. Unit tests guard the registry. |
| Editing live gitignored config drifts from the example configs | Update `config.example.*`/`routing.example.yaml` in the same change so the committed surface documents the real topology. |
| LiteLLM transient unavailability misread as backend failure | Matrix distinguishes gateway-availability failure from backend incompatibility (FR-007). |
| `spark/gpt-oss` empty-content at low max_tokens (reasoning eats budget) | Set adequate max_tokens in the matrix + per-role config; documented in research.md. |
| Disrupting the running daemon mid-migration | Matrix uses throwaway containers + separate `--config` (FR-008); live re-point is a deliberate, validated step with the daemon restarted once at the end. |

## Post-Design Constitution Re-check

After Phase 1 (data-model/contracts/quickstart): still **PASS** — no new deps, no persisted schema,
net reduction in moving parts, test-/matrix-gated, scope bounded. Complexity Tracking not required.
