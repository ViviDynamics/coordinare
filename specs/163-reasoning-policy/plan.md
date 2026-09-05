# Implementation Plan: Reasoning Policy and Truncation Classification

**Branch**: `163-reasoning-policy` | **Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md)
**Advances**: #244

## Summary

Two halves. **US1** stops coordinare misreporting a budget-exhausted reasoning response as
malformed output, for every role rather than one. **US2/US3** let a model declare how its
reasoning should be produced, so the situation stops arising where the model cooperates —
with the known-harmful case unable to be enabled by accident.

US1 is pure coordinare-side, needs no model or CLI cooperation, and is independently
shippable. **US2 is unblocked**: research R4 established from the code that the shim can
carry the four direct-routed backends, so it becomes the single injection point for every
backend and a stale routing exception retires with it.

The deceptively small part is US1. "Ungate a classifier" sounds like deleting two
conditions; it is not, because both call sites derive *behaviour* from the classifier's
result and would silently widen it. That is the main design content below.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — `coordinare.services.assessor_failure`, the
`monitor_performer` / `handle_system_error` graph nodes, the 080 model catalog in
`coordinare.config`, and (for US2) the performer proxy shim. **No new dependencies.**
**Storage**: N/A. No persisted state, no `state_store.py` change, no schema bump. The
reasoning policy is a config surface; the classification is computed per failure.
**Testing**: pytest, `tests/unit/test_163_*.py` per repo convention.
**Target Platform**: unchanged.
**Project Type**: single.
**Constraints**: Additive. A model with no declared policy must produce a byte-identical
request to today's. No gateway deployment change. Verification through the gateway only.
**Scale/Scope**: ~10 roles, 3 served self-hosted models.

## Constitution Check

Evaluated against `.specify/memory/constitution.md` v1.1.0.

| Gate | Status | Notes |
| --- | --- | --- |
| 1. Lint & Format | Planned | `make lint` / `make fmt`. |
| 2. Type Check | Planned | mypy configured; new code fully annotated. |
| 3. Unit Tests | Planned | `make test`; no skipped tests introduced. |
| 4. Integration Tests | Planned | `make test-all` before PR. |
| 5. Coverage Check | Planned | New code ships with tests. |
| 6. Performance Check | N/A | Not a performance path. |
| 7. Accessibility Check | N/A | No UI change. |
| 8. Code Review (non-author approval) | **DEVIATION** | Solo-authored; substituted with the mandatory adversarial `Workflow` review over the full diff. A substitution, not an equivalent. Recorded, not skipped. |

**Principle V (Clarity Before Action)**: the three decisions that shape scope (gateway
versus request, both halves or one, ordering) were settled with the requester before
drafting. A fourth — how to handle the injection-point gap — was resolved from the code in
R4 rather than assumed or deferred to an experiment.

## The two traps in "just ungate the classifier"

Both call sites use the classifier's result to drive behaviour, not only to report. Widening
the classifier without scoping those derivations changes semantics far beyond this feature.

**Trap 1 — ENV_BLOCKED would widen.** `handle_system_error.py` (~138):

```python
assessor_shape = classify_assessor_failure(reason) if stage == "assessing" else None
is_env_blocked = assessor_shape == "empty_body"
```

Ungate naively and *any* stage whose reason matches `_EMPTY_BODY_MARKERS` becomes
ENV_BLOCKED. That is a spec-095 operator-facing state meaning "infrastructure, the card
auto-resumes". Handing it to every stage is a routing change this feature never asked for.

**Trap 2 — retryability would widen.** `monitor_performer.py` (~4537):

```python
if (reason.startswith(_FORMAT_ERROR_PREFIX)
        or _is_transient_backend_error(reason)
        or _is_format_contract_error(reason)
        or assessor_shape is not None):
```

`assessor_shape is not None` is a retry trigger. Ungating makes **every** shape — including
`malformed_body` — retryable for **every** stage. Spec 119 deliberately made malformed
output retryable for one role via `_is_format_contract_error`; this would silently
generalise that decision to all of them.

**How the plan avoids both.** Separate *classification* from *derivation*:

- `classify_assessor_failure` is called for all stages (US1's actual requirement).
- The two behavioural derivations keep their current reach explicitly:
  `is_env_blocked` stays `stage == "assessing" and shape == "empty_body"`;
  the retry trigger stops using `shape is not None` and names the shapes it means.
- Each derivation gets a regression test asserting non-assessing stages are unchanged.

Widening either derivation is a separate, evidence-backed decision — out of scope here and
recorded as such.

## FR-008: truncation must not retry unchanged

Once truncation is classified for all stages, Trap 2's path would make it retryable. An
identical retry of a truncated request truncates identically — same prompt, same budget —
so the retry burns the `system_error_count` budget and lands in the same place, having
turned a wrong verdict into a slow wrong verdict.

The plan therefore treats `truncated` as a shape that either changes something (a larger
output budget on the retry) or does not retry and reports the budget plainly. Which of the
two is a design decision for tasks, but "retry unchanged" is excluded either way.

## Project Structure

### Documentation (this feature)

```
specs/163-reasoning-policy/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── checklists/requirements.md
```

### Source Code

```
US1 (P1 — shippable now)
src/coordinare/services/assessor_failure.py   # EDIT: accept a structured finish reason,
                                             #       keep prose markers as fallback
src/coordinare/graph/nodes/monitor_performer.py    # EDIT: ungate the call; name the shapes
                                             #       the retry trigger means (Trap 2)
src/coordinare/graph/nodes/handle_system_error.py  # EDIT: ungate the call; keep
                                             #       is_env_blocked scoped (Trap 1)

tests/unit/test_163_truncation_classification.py  # every role, structured + prose paths
tests/unit/test_163_derivation_scope.py           # Traps 1 and 2 stay scoped

US2/US3 (P2 — unblocked; routes the four direct backends through the shim)
routing.yaml / routing.example.yaml            # EDIT: entries for openclaw + codex,
                                             #       retiring the stale 122 exception
src/coordinare/models/config.py (or config.py)     # ModelEndpoint gains an optional policy
src/coordinare/config.py                           # resolve_performer_dispatch_model
                                             #       surfaces it into dispatch context
agent/performer/src/performer/proxy/shim.py       # applies the policy to the outbound body
benchmarks/…/config.example.yaml                  # document the surface generically

tests/unit/test_163_reasoning_policy.py           # declared/absent/unknown-value
tests/unit/test_163_policy_safety.py              # US3: the harmful case is pinned
```

**Structure Decision**: single project, editing in place. US1 touches three existing files
and adds no module — the classification vocabulary already exists and is correct; only its
reach and its inputs change. Building a parallel classifier would be the wrong shape.

## Phase 0: Research

See [research.md](./research.md). R1-R3 are settled by measurement; R4 and R5 by reading the
code. **All resolved — nothing gates implementation.**

- **R1**: the failure is real on the current model (measured).
- **R2**: the preventative flag is model-dependent and harmful on the default model
  (measured, reproduced twice) — hence per-model, default off.
- **R3**: the CLIs cannot carry the flag. `chat_template_kwargs` is a vLLM body extension;
  coordinare's codex writer actively prevents extra TOML keys; openclaw's provider config is
  a fixed schema; no passthrough hook is documented for either.
- **R4 (RESOLVED)**: the shim CAN carry the four direct-routed backends. `openclaw` is in
  `PROVIDER_BASE_URL_ENV`, `UNSUPPORTED_BACKENDS` is empty, `launch.py` names openclaw as a
  bare-root CLI, and the shim serves the un-prefixed `/chat/completions` it posts to. The
  404 behind the exception was fixed in spec 122 itself (`a63fe63`) — and `opencode`, named
  in the same comment as sharing that cause, runs through the shim in production today. So
  US2 routes the four direct backends through the shim.

## Phase 1: Design

See [data-model.md](./data-model.md) and [quickstart.md](./quickstart.md).

## Complexity Tracking

| Deviation | Current need | Why the simpler option is insufficient |
| --- | --- | --- |
| Splitting classification from its two derivations | FR-001 widens classification; nothing in the spec widens ENV_BLOCKED or retryability | Reusing `shape is not None` as a retry trigger silently generalises a spec-119 decision to every role, and `shape == "empty_body"` silently generalises a spec-095 operator state. Both are behaviour changes disguised as a refactor. |
| US2 adds routing entries for two backends rather than only adding a config field | The only injection point (the shim) does not currently cover the four roles that need it most | A policy that silently skips reviewer, security, architect and implementer would ship a feature whose headline claim is false for the roles where a wrong verdict costs most. Routing them through the shim also retires an exception whose cause was fixed in the same spec that created it. |

## Deferrals

None hidden. Two things are explicitly **not** in this feature and are recorded rather than
assumed away:

- Widening ENV_BLOCKED or retryability to non-assessing stages. Possibly desirable,
  separately decidable, needs its own evidence.
- Opting any model in to a reasoning policy. This feature builds the mechanism and records
  the evidence; enabling it for a model is a separate decision (spec FR/US3).

**US2/US3 are unblocked.** R4 resolved the injection-point question from the code. US1 does
not depend on US2 and should still ship first, because it protects every role regardless of
what any model or backend does.
