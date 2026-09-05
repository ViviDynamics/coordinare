# Implementation Plan: Role-workflow plugin layer, with QA as the first workflow

**Branch**: `164-qa-role-workflow` | **Date**: 2026-09-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/164-qa-role-workflow/spec.md`

## Summary

Add a role-workflow layer inside the performer, sitting between the
coordinare/performer contract and the work itself. A role with a workflow
configured runs an ordered sequence of small, individually verifiable steps
instead of one backend invocation followed by post-hoc parsing. Coordinare still
dispatches a task and receives one `PerformerResponse`; nothing about the
contract changes.

QA is the first workflow: plan → baseline → execute → observe → judge → report,
each step carrying its own persona and output schema, plus a structured repair
brief (`qa_findings`) emitted alongside the verdict for the next implementer
round.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — pydantic 2.x (step/finding/plan models),
httpx (model calls via the LiteLLM gateway), Playwright + bundled Chromium
(already in the performer image), structlog (step observability), the existing
`BackendAdapter` registry, `qa_capture`, `cdn_upload`, `qa_verdict`.
**New dependencies**: none required. See research.md R1 for the runner decision
(plain async step functions, not langgraph).
**Storage**: N/A. A workflow run is one-shot and dies with the container. No
coordinare state, no `state_store.py` schema change.
**Testing**: pytest with recorded model-response fixtures (deterministic); a
separate on-demand scenario eval harness (nondeterministic, not a CI gate).
**Target Platform**: the Debian-based performer container; workflow steps are
unit-testable on the host without a container.
**Project Type**: single (performer package + coordinare config surface)
**Performance Goals**: see Constitution Check gate IV below.
**Constraints**: all model traffic through the LiteLLM gateway; workflows load
only from the performer's trusted package directory, never from a cloned repo.
**Scale/Scope**: one layer, one workflow, six steps, six scenario fixtures.
Redistributes ~498 existing lines from `main.py` rather than adding net logic.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

### I. Code Quality First — PASS, and directly advanced

The spec's core motivation is a Single Responsibility violation: the QA role
occupies 498 lines of `main.py:2981-3479` inside a 4,316-line file, and the QA
persona cannot be described in a single sentence. The design decomposes both.
**Minimal dependencies** is why research.md R1 rejects langgraph for this layer
despite coordinare using it: a one-shot in-container run uses almost none of what
it provides.

### II. Testing Discipline — PASS with a documented boundary

The constitution requires deterministic tests and forbids flaky ones. The spec's
scenario eval (FR-022) calls a real model and is nondeterministic by nature.
These are reconciled by keeping them separate, not by relaxing the rule:

- **The test suite** is deterministic. Model-dependent steps run against
  recorded response fixtures, including the malformed and truncated ones
  captured during design. It runs in CI and gates merges.
- **The scenario eval** is a measurement harness, not a test. It runs on demand,
  reports a pass rate per scenario, and gates nothing.

No flaky test enters CI. This boundary MUST be stated in the eval's own README
so a later contributor does not "helpfully" wire it into CI.

### III. User Experience Consistency — PARTIALLY APPLICABLE

No new end-user interface. Two clauses do apply:

- **Error communication**: the repair brief (FR-012) is the actionable-error
  requirement applied to an agent consumer. It must say what failed and how to
  reproduce, without leaking raw stack traces as the whole message.
- **Loading and state feedback**: satisfied by FR-008, step transitions emitted
  as `BackendEvent`s so the dashboard shows progress rather than a long silence.

Accessibility, design tokens and responsive behaviour are not applicable.

### IV. Performance by Design — GATE FINDING, resolved here

The constitution requires measurable performance budgets in the spec's Success
Criteria. **The spec as committed does not have them**, and the design knowingly
adds cost (a second boot for the baseline). Budgets are therefore defined here
and MUST be back-filled into the spec's Success Criteria before implementation:

| Budget | Value |
|---|---|
| Baseline measurement | Current QA stage p50/p95 duration MUST be measured over at least 10 real cards before implementation, so the below are anchored to a real number rather than a guess. |
| Non-visual cards | Workflow p50 ≤ 2× measured current QA p50. |
| Visual cards | Workflow p50 ≤ 3× measured current QA p50 (accounts for the merge-base boot). |
| Baseline skip | Baseline step MUST NOT run when the plan contains no visual or flow checks; verified by test, not by inspection. |
| Model-call ceiling | ≤ 12 model calls per QA run, enforced in code. A runaway workflow fails loudly rather than burning the gateway. |
| Per-call token budget | Judgment steps ≥ 3000 max_tokens, observation steps ≥ 1500. Measured during design: 500 truncated mid-JSON, 3000 completed cleanly. |
| Outcome metric | Rounds-to-green per issue, emitted by the workflow (spec success metric). |

Regression prevention: the model-call ceiling and the baseline-skip rule are
covered by deterministic unit tests. Wall-clock budgets are tracked by the eval
harness, not by a CI benchmark — a gateway-dependent timing assertion in CI
would itself be a flaky test, which II forbids.

### V. Clarity Before Action — PASS

Design-time ambiguities were resolved and recorded rather than assumed:
plugin ownership (operator-owned, coordinare config), suite role (on-demand
scored eval), and the repair brief's register (descriptive, not prescriptive)
are all settled in the spec. The three items under "Deferred decisions" are
**decisions with recorded defaults**, not `NEEDS CLARIFICATION` blockers, and
research.md records the rationale for each. No requirement is tagged
`NEEDS CLARIFICATION`; implementation is not blocked.

## Project Structure

### Documentation (this feature)

```text
specs/164-qa-role-workflow/
├── spec.md              # Feature specification
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks — not created here)
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py           # registry: name → workflow class (mirrors backends/)
│   ├── base.py               # RoleWorkflow protocol + WorkflowResult
│   ├── toolkit.py            # run_command / capture / dom_snapshot / call_model / get_backend
│   ├── budget.py             # per-step token budget, truncation retry, call ceiling
│   ├── schema_guard.py       # per-step schema validation + single reprompt
│   └── qa/
│       ├── __init__.py       # QAWorkflow: sequences the six steps
│       ├── plan.py           # step 1  (model)
│       ├── baseline.py       # step 2  (no model)
│       ├── execute.py        # step 3  (mixed)
│       ├── observe.py        # step 4  (model) + pooling
│       ├── judge.py          # step 5  (model, text only)
│       ├── report.py         # step 6  (assembles PerformerResponse + qa_findings)
│       └── personas.py       # the six per-step personas
├── main.py                   # dispatch: use workflow when configured, else today's path
└── backends/                 # unchanged; workflows call these as primitives

src/coordinare/
├── models/config.py          # role gains an optional `workflow` name
└── graph/nodes/
    └── dispatch_performer.py # carry qa_findings into card_context (scanner_findings pattern)

tests/
├── unit/workflows/           # per-step tests, recorded fixtures
└── eval/qa_scenarios/        # on-demand scored eval + fixture manifests (NOT in CI)
```

**Structure decision**: the layer lives beside `backends/` and mirrors its
registry pattern, because it is the same kind of thing — a named, swappable
strategy the performer selects at start. The QA workflow is a subpackage so each
step is its own small file, satisfying Principle I where the current 498-line
block does not.

## Complexity Tracking

| Item | Why it is justified | Simpler alternative rejected because |
|---|---|---|
| A second execution path for roles | Required for opt-in rollout; reviewer/security/documenter keep working while QA is rebuilt | Migrating all roles at once risks every stage simultaneously, on a mechanism not yet proven |
| Six steps instead of one prompt | Each named failure mode (budget, schema, pooling, plan) needs somewhere to live | A longer persona was the status quo, and produced a 2% sweep pass rate |
| Separate eval harness | Constitution II forbids flaky CI tests; the scenario suite is inherently nondeterministic | Putting scenarios in CI would either be flaky or require asserting on exact model strings |

**Debt acknowledged**: two execution paths must not be permanent. "Done" for the
layer is every role either migrated or carrying a documented decision to stay
single-prompt.
