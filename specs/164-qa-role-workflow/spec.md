# Spec 164: Role-workflow plugin layer, with QA as the first workflow

**Status**: draft, awaiting review
**Related**: #255 (QA flow validation), #256 (documenter as single writer),
spec 120 (QA evidence integrity), spec 083 (`scanner_findings` precedent),
spec 124 (documenter), spec 161 (bench harness)

## Problem

QA is a multi-step reasoning job compressed into a single prompt. It must read a
diff, work out what needs checking, check it, and judge whether the feature is
complete. None of that fits in one instruction, and the evidence says it does not
fit today:

- QA is the second-hardest persona in the 077 sweep (~2% pass rate).
- The QA role already carries **498 lines of post-processing** in
  `agent/performer/src/performer/main.py:2981-3479`, inside a 4,316-line file.
  The logic did not fail to exist; it outgrew the persona and spilled into an
  if-chain, where it can only parse what the agent happened to return.
- Spec 120 exists because QA produced confident false passes.

The role fails on four axes at once — false passes, missed regressions,
non-convergence, and checking the wrong things. A component failing on every axis
usually has one structural cause rather than four bugs. The structural cause is
that there is nowhere to put sequencing, budget control, retry policy, or
per-step verification except inside a prompt or inside `main.py`.

### Measured during design (2026-09-04)

A prototype of the proposed before/after pipeline was run against the live
LiteLLM gateway (`spark/glm-5.3-flash`) on rendered UI screenshots:

- **The concept works.** Given only structured descriptions of a before and an
  after screenshot, plus the claimed change, the model correctly returned
  `verdict: unexpected_regression` and identified a silently deleted password
  field as an unexpected change. Today's single-screenshot QA is structurally
  blind to that defect, because one image with no baseline has nothing to
  regress against.
- **The plumbing failed twice, structurally.** A consensus-pooling rule keyed on
  exact labels discarded the feature under test (three runs saw the new dropdown
  and named it `workspace`, `workspace / acme hq`, `acme hq`; requiring two
  identical labels kept none), making a good change look like no change at all.
  Separately, a stage died at `finish_reason: length` with empty content, all
  6000 tokens consumed by reasoning (the #244 trap). The description stage also
  emitted element kinds outside the schema it was given.

Pooling policy, token budgeting, schema enforcement, retry-on-truncation and
per-step verification. Five concerns, none promptable.

## Goals

- **G1** Give roles a place to express multi-step work, without changing the
  coordinare/performer contract.
- **G2** Rebuild QA on that mechanism so it plans, baselines, executes, observes
  and judges as distinct verifiable steps.
- **G3** Turn QA's output into an actionable repair brief for the implementer,
  not just a verdict.
- **G4** Make QA's behaviour measurable, so "better" is falsifiable.

### Success Criteria

Constitution Principle IV requires measurable performance budgets in the spec.
These were defined in `plan.md`'s Constitution Check and are back-filled here
(the adversarial review confirmed their absence as a live gap, not a formality).

| Criterion | Target | Status |
|---|---|---|
| Baseline measurement | Current QA stage p50/p95 over ≥10 real cards | **Not yet measured** (T001b) — the budgets below are provisional until it is |
| Non-visual cards | Workflow p50 ≤ 2× measured current QA p50 | Provisional |
| Visual cards | Workflow p50 ≤ 3× measured current QA p50 (accounts for the merge-base boot) | Provisional |
| Baseline skip | Baseline MUST NOT run when the plan has no visual or flow checks | **Enforced by test** |
| Model-call ceiling | ≤ 12 model calls per QA run, enforced in code | **Enforced by test** |
| Per-call token budget | Judgment ≥ 3000 max_tokens, observation ≥ 1500 | **Enforced by test** |
| Scenario verdicts | All six scenarios produce the correct verdict class for the correct reason | **Met** — 6/6 on the live eval |
| Outcome metric | Rounds-to-green per issue, emitted by the workflow | **Emitted** (`WorkflowMetrics.round_number` / `reached_green`) |

The wall-clock rows are deliberately relative and provisional. Anchoring them to
a number nobody has measured would be a budget in name only; T001b closes them.

### Success metric

**Rounds-to-green per issue**, not QA wall-clock time. A slower QA that returns a
precise repair brief shortens overall cycle time by avoiding another blind
implementer round. The workflow emits this metric rather than leaving it to be
inferred from board history.

Secondary: per-scenario pass rates from the scenario eval (below).

## Non-goals

- Changing the coordinare/performer contract in any way.
- Migrating other roles. QA first; the seams stay clean for others (#256 is a
  candidate consumer, explicitly deferred).
- Adopting BrowserOS or any second browser (see #255 for the analysis).
- Cloud models. All traffic through the LiteLLM gateway.

## Architecture

### The seam

Today a performer run is: set up the workspace, invoke one backend, run the
role's post-processing block, emit a `PerformerResponse`.

A **role workflow** replaces the middle two for roles that have one configured.
It returns the same `PerformerResponse` shape, so everything outward — the
`BackendAdapter` protocol, coordinare's dispatch, `qa_verdict` — is untouched.

- **FR-001** A role workflow runs entirely inside the performer. Nothing calls
  coordinare mid-run. Coordinare dispatches a task and receives one response,
  exactly as now.
- **FR-002** A workflow receives a toolkit of primitives, not a framework:
  `run_command`, `capture_screenshot` (existing `qa_capture`), `dom_snapshot`,
  `call_model(persona, schema)` for a scoped judgment, and `get_backend(...)`
  when a step genuinely needs an agent turn. Backends remain execution
  primitives; the workflow decides which primitive each step needs.
- **FR-003** Workflow selection is coordinare-side configuration, named per role
  alongside the existing `backend`, and travels to the performer at start.
- **FR-004** The performer loads workflows only from its own trusted package
  directory. A cloned repository can never introduce, alter, or parameterise a
  workflow as code. (Trust boundary; see spec 144, spec 131.)
- **FR-005** A role with no workflow configured behaves exactly as today: one
  backend invocation plus existing post-processing. Opt-in per role, default off.

### Personas are scoped, not removed

The persona still matters — the model must know it is doing QA work. What
changes is granularity. Instead of one persona holding "read the diff, derive
checks, boot, capture, compare, judge, and emit this JSON", each step carries a
small persona that says one thing.

- **FR-006** Each workflow step supplies its own persona and its own output
  schema. The graph is the durable contract; the persona says which chair the
  model is sitting in.

This follows what the project already learned: models forget early instructions
over long runs, so decompose into focused sub-tasks against durable external
contracts.

### Trust model

Process autonomy, output accountability.

- **FR-007** The workflow owns *how* QA is performed. Coordinare owns *what
  counts as a valid verdict* and continues to gate on it via `qa_verdict`
  unchanged. Without that line, the least-trusted component is also its own only
  check.
- **FR-008** The workflow emits stage transitions as `BackendEvent`s, giving
  coordinare and the dashboard visibility without control.

## The QA workflow

Organising principle: **the model plans and witnesses; code decides and
verifies.** Anything the DOM, an exit code, or a file on disk can answer
deterministically is taken from there.

1. **Plan** *(model, small persona)* — from the diff and acceptance criteria,
   emit a test plan: checks tagged `command` / `flow` / `visual`, each bound to
   the criterion it serves. Runs before any execution so it cannot be retrofitted
   to whatever happened to pass. Emitted as an event; inspectable.
2. **Baseline** *(no model)* — check out the merge-base, boot, capture DOM and
   screenshots for the surfaces the plan names. Skipped when the plan contains no
   visual or flow checks.
3. **Execute** *(mixed)* — commands yield real exit codes. Flows run through
   Playwright driven by the declarative steps the plan produced: the model
   decides *what*, our code owns *how*. This is the lesson `qa_capture.py`
   already encodes — small models drive browsers badly, and a multi-step flow is
   strictly harder than the single screenshot that already needed a backstop.
4. **Observe** *(no model)* — read each planned surface from the base app and
   the head app via the DOM, and diff them. Labels come from the DOM, which
   knows them exactly; a surface that renders nothing on both sides is reported
   as unobservable rather than compared.
5. **Judge** *(model, text only)* — per-criterion verdicts each bound to an
   executed check, plus the before/after delta against the claimed change with
   unexpected changes called out.
6. **Report** — assemble the response; publish evidence through the existing
   `qa-assets` upload path (`cdn_upload.py`).

Cross-cutting, owned by the workflow and impossible in a prompt:

- **FR-009** A per-step token budget, with retry at a larger budget on
  `finish_reason: length`. A truncation must never surface as malformed output.
- **FR-010** Per-step schema validation, with exactly one reprompt on violation,
  then a loud failure.
- **FR-011** Before/after comparison reads element kind, position and label
  from the DOM directly. Labels come from the DOM, never from a model. *(The
  original text described pooling repeated model descriptions; that path was
  superseded by direct DOM reads, found never to be invoked in the second review
  round, and removed as dead code. The lesson it encoded -- never key a
  comparison on model-supplied labels -- lives in `observe.py`.)*

## Two outputs

QA produces a verdict for the gate **and** a repair brief for the next
implementer round.

The precedent exists: `scanner_findings` is a structured channel from the
security stage into the implementer's dispatch context
(`dispatch_performer.py:1355-1357`). `card_context` carries `scanner_findings`,
`repair_mandate`, `disputed_feedback`, `relay_feedback` and
`prior_clarifications` — and nothing from QA. QA failures reach the next round as
prose, if at all.

- **FR-012** The workflow emits structured `qa_findings` mirroring the
  `scanner_findings` shape. Per failure: the criterion, the plan step that
  failed, the command with exit code and output excerpt, observed versus
  expected, the implicated file or hunk where derivable, and a reproducing
  command. The plan artifact rides along so the implementer can reproduce rather
  than guess.
- **FR-013** The brief carries evidence, not only conclusions. A precise brief
  from a wrong QA is worse than a vague one, because the implementer will
  confidently implement the wrong fix; shipping exit codes and captures lets the
  next stage disagree.
- **FR-014** The brief reports; it does not prescribe. What failed and how to
  reproduce, not how to fix. *(Decision recorded as descriptive. Revisit if QA
  proves reliable enough to suggest fixes.)*

## Testing

### Unit

Each stage is a function with inputs and outputs, testable on the host without a
container. Model-dependent stages use recorded fixtures — the design-session
transcripts, including the malformed ones — so the suite is deterministic and
does not touch the gateway.

Regressions drawn directly from the measured failures above:

- **FR-015** Three descriptions naming one element differently pool to one
  observation, not zero.
- **FR-016** A `finish_reason: length` response retries at a larger budget and
  never surfaces as malformed output.
- **FR-017** Out-of-schema output triggers one reprompt, then fails loudly.
- **FR-018** An element present in before and absent in after yields
  `unexpected_regression`.

Mutation discipline applies: a test enforcing a rule is broken once per instance
of that rule, mutated in the real tree. This matters most for the evidence floor
and the pooling threshold, which pass vacuously if only the example in front of
them is tested.

### Scenario eval

Generated stub repositories, each with a base commit, a head commit, acceptance
criteria, and an expected verdict class.

| Scenario | Head commit | Expect |
|---|---|---|
| Healthy | adds the feature, breaks nothing | pass |
| Regression | adds the feature, breaks prior behaviour | fail, naming the broken thing |
| Misplaced | right thing, wrong place | fail, naming the wrong location |
| Incomplete | satisfies 2 of 3 criteria | fail (not a pass) |
| Cosmetic no-op | claims the feature, changes nothing functional | fail |
| Env-broken | app will not boot | `environment_error`, not a verdict |

- **FR-019** Assertions are qualitative: the verdict class is correct, the
  failure names the correct artifact, and a healthy change is never failed.
  Exact strings are not asserted; model output is nondeterministic.
- **FR-020** Fixtures are generated from a manifest at test time, never
  committed. A git repository nested inside this repository is a hazard.
- **FR-021** Stub apps are minimal Python HTTP servers. Python is already in the
  image and they boot in milliseconds; the scenarios test QA's reasoning, not a
  web framework.
- **FR-022** The suite runs on demand as a scored eval: N repeats per scenario,
  a pass rate per scenario, tracked over time. It is not a CI gate — one flaky
  sample must not block unrelated work, and a rate is the only form that can show
  whether a change made QA better or worse.

**These scenarios are the acceptance criteria for the enhancement.** It ships
when the suite produces the right verdict classes, not when the code is written.

## Scoping note

FR-001 to FR-008 describe the mechanism; FR-009 to FR-022 describe QA as its
first consumer. They are specified together because the mechanism's shape cannot
be validated without a real consumer, and QA is the one driving the need. If the
implementation plan grows unwieldy, the natural split is layer-then-workflow,
in that order, with the layer landing default-off and inert until QA uses it.

## Rollout

Opt-in per role, default off. Reviewer, security and documenter keep working
unchanged while QA is rebuilt. The 498 lines in `main.py` are not deleted; they
are distributed into the steps that should have owned them, where each piece
becomes testable on its own.

**Known cost**: this introduces a second way for a role to execute, and two paths
mean two things to maintain. Acceptable while QA proves the mechanism;
not acceptable permanently. "Done" for the layer is every role either migrated or
a documented decision that it stays single-prompt.

## Deferred decisions

- **Runner**: whether steps execute under langgraph (one mental model shared with
  coordinare) or plain async step functions (a one-shot in-container run uses
  perhaps a tenth of what langgraph provides). The graph's *shape* is the design
  decision; the runner is deferrable and does not block implementation.
- **Baseline cost**: checking out and booting the merge-base roughly doubles
  environment time on visual cards. Partly mitigated by skipping it when the plan
  has no visual checks; measure before optimising further.
- **Plan-stage risk**: a confidently wrong plan yields confidently wrong QA.
  Mitigated by treating the plan as evidence — a criterion with no plan entry and
  no executed check cannot be counted as passed — which folds into the existing
  evidence floor rather than inventing a second gate.
