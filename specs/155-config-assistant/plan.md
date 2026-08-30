# Implementation Plan: Config Assistant

**Branch**: `155-config-assistant` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)
**Issue**: #202

## Summary

A dashboard chat panel that helps an operator reach a working configuration by **proposing**
changes they apply. The assistant reads a masked view of the config and answers with one
constrained JSON object per turn — not an agentic tool loop, because spec 124 established that
tool-calling is unreliable on the self-hosted models this must support (research R1).

The model is the conducting backend coordinare already configures, so BYO-model and "no cloud
requirement" need no new surface (R2). "Never applies" is structural: the module has no write
path (R3).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12). Dashboard front end is vanilla
ES5-style JS inlined in `dashboard.py`, matching what is already there.
**Primary Dependencies**: existing only — `config_descriptors` (masking + typed sections),
`config_write_service` (`guard_concurrency`, `atomic_write_yaml`), `services/conducting`
(`ConductingBackendProtocol`), FastAPI/Starlette, structlog. **None added.**
**Storage**: none. Conversation is session-scoped and unpersisted (FR-015).
**Testing**: pytest, `tests/unit/`. The backend is scripted, so no model and no network.
**Target Platform**: the existing dashboard, same trust boundary (spec 144).
**Project Type**: single
**Performance Goals**: one model call per turn; no background work when disabled.
**Constraints**: no existing dashboard test modified (SC-008); dashboard unchanged when the
feature is off (SC-005).
**Scale/Scope**: one new service module, one new API surface, a dashboard panel, one small
change to an existing endpoint.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality First | Passes. Reuses the existing masking and write primitives rather than adding parallel ones (R4, R5); the assistant module has a single responsibility — turn a conversation into a validated proposal. |
| II. Testing Discipline (NON-NEGOTIABLE) | Tests first. Deterministic: the conducting backend is scripted, so no model, no network, no sleeps. The two safety properties (no write path, no secret leakage) are asserted structurally, not observationally. |
| III. User Experience Consistency | Central. Proposals render as diffs in the existing config surface; malformed model output surfaces as a plain failure (FR-012) rather than a broken proposal; the operator is told what the assistant could not see (FR-017). |
| Minimal dependencies | Passes — none added. |

No violations.

## Project Structure

```
src/coordinare/
├── services/config_assistant.py   # NEW: masked context, prompt, parse, validate. No write path.
├── dashboard.py                   # chat endpoints + panel; optional expected_hash on the
│                                  #   existing PUT /api/config/global (R5)
└── config.py                      # the off-by-default flag

tests/unit/
└── test_155_config_assistant.py   # NEW
```

## Phases

**Phase 1 — Tests first.** The safety properties before anything else: no write path in the
module (AST), no secret value in any prompt the backend receives, malformed output yields no
proposal, unknown section/field rejected, disabled means absent.

**Phase 2 — The assistant service.** Masked context from descriptors; a bounded prompt that
states omissions; one `prompt(..., response_format="json")` call; parse into reply-or-proposal;
validate a proposal against the real config models.

**Phase 3 — The apply path.** Optional `expected_hash` on `PUT /api/config/global`, enforced with
the existing `guard_concurrency` → 409. Optional so no existing caller changes (SC-008).

**Phase 4 — The dashboard panel.** Chat UI in the existing inline-JS style; proposals render as a
diff with an Apply button that posts the proposal plus the hash it was built against.

**Phase 5 — First run.** With an unconfigured install, open on the golden path rather than a
blank prompt.

**Phase 6 — Verify and review.** Full suite, lint, mypy, confirm SC-005/SC-008 by diff, then the
mandatory adversarial `Workflow` review before merge.

## Risks

- **Building the thing spec 124 already disproved.** The largest risk, and the reason R1 is a
  written decision rather than an implementation detail. A reviewer who "restores" tool-calling
  would reintroduce it.
- **A second masking implementation.** Would drift from the UI's and leak. Mitigated by reusing
  `serialize_value` and asserting a real secret never appears in prompt text.
- **The guard that was assumed to exist.** R5: the global config write has none today. If the
  assistant's Apply reused it unchanged, FR-007 would be silently unmet — the exact shape of
  defect this project keeps finding by execution rather than reading.
- **Scope leak into the config UI.** Tightening the existing UI's writes is a real improvement
  and not this spec's business; filed separately rather than done quietly here.
