# Phase 1 Data Model: Spec 164

No persisted coordinare state. Every entity below lives for the duration of one
performer run except `RoleConfig.workflow` (config) and `qa_findings` (carried
across stages in `card_context`, exactly as `scanner_findings` already is).
`state_store.py` is untouched; there is no schema-version bump.

## RoleConfig.workflow *(coordinare config)*

| Field | Type | Notes |
|---|---|---|
| `workflow` | `str \| None` | Optional. Names a registered workflow. `None` (default) means today's single-backend path. Validated against the registry at config load, like `backend`. |

## RoleWorkflow *(protocol)*

Implemented by each workflow. Deliberately narrow — the performer needs to
start one, watch it, and stop it, which is the same shape `BackendAdapter`
already has.

| Member | Signature | Notes |
|---|---|---|
| `run` | `async (stand, score, toolkit) -> WorkflowResult` | Executes all steps in order. |
| `name` | `str` | Registry key. |

## WorkflowResult

| Field | Type | Notes |
|---|---|---|
| `report` | `dict` | The role's structured report — for QA, the same shape `main.py` already builds a `PerformerResponse` from. |
| `findings` | `list[Finding]` | Structured repair brief. Empty on success. |
| `events` | `list[BackendEvent]` | Step transitions, already drained by the existing event path. |
| `metrics` | `WorkflowMetrics` | Step timings, model-call count, retry count. |

## WorkflowMetrics

| Field | Type | Notes |
|---|---|---|
| `model_calls` | `int` | Enforced against the ceiling (12). |
| `truncation_retries` | `int` | `finish_reason: length` retries taken. |
| `schema_reprompts` | `int` | Schema violations reprompted. |
| `step_durations_ms` | `dict[str, int]` | Per-step wall clock, feeds the performance budgets. |
| `baseline_skipped` | `bool` | True when the plan had no visual or flow checks. |

## TestPlan *(QA step 1 output)*

| Field | Type | Validation |
|---|---|---|
| `checks` | `list[PlanCheck]` | Non-empty, or the step fails closed (R7). |
| `surfaces` | `list[str]` | Routes/URLs the baseline step must capture. Empty ⇒ baseline skipped. |

### PlanCheck

| Field | Type | Validation |
|---|---|---|
| `id` | `str` | Unique within the plan. |
| `criterion` | `str` | Must correspond to an acceptance criterion. |
| `kind` | `"command" \| "flow" \| "visual"` | Closed enum. |
| `command` | `str \| None` | Required when `kind == "command"`. |
| `steps` | `list[FlowStep] \| None` | Required when `kind == "flow"`. |

### FlowStep

Declarative on purpose: the model decides *what*, our Playwright code owns *how*.

| Field | Type | Validation |
|---|---|---|
| `action` | `"goto" \| "fill" \| "click" \| "expect_text" \| "screenshot"` | Closed enum. Anything else is a schema violation. |
| `target` | `str \| None` | Selector or URL. |
| `value` | `str \| None` | For `fill` / `expect_text`. |

## ExecutedCheck *(QA step 3 output)*

| Field | Type | Notes |
|---|---|---|
| `plan_check_id` | `str` | Binds evidence to the plan entry. A criterion with no bound `ExecutedCheck` cannot be counted as passed. |
| `command` | `str` | As run. |
| `exit_code` | `int` | Real, never inferred. |
| `output_excerpt` | `str` | Truncated, secret-redacted. |
| `passed` | `bool` | Derived from `exit_code` / assertion, not from model opinion. |

## Observation *(QA step 4 output)*

Read from the DOM by the reader in `workflows/qa/dom.py`; no model is involved
(the model-description pooling path was removed as dead code in the second review
round -- see R3).

| Field | Type | Notes |
|---|---|---|
| `kind` | closed enum | Element kind, from the reader's tag mapping (`select`⇒`dropdown`, `input[type=password]`⇒`password_input`, …). |
| `position` | `int` | Document order. Breaks ties between unlabelled elements of one kind only; never part of the identity of a labelled element, or an inserted field makes everything below it look removed. |
| `label` | `str \| None` | **From the DOM**, never from the model. With `kind`, the diff identity. |

## VisualDelta *(QA step 5 input)*

| Field | Type | Notes |
|---|---|---|
| `added` | `list[Observation]` | Present in after, absent in before. |
| `removed` | `list[Observation]` | Present in before, absent in after. The regression signal. |
| `layout_defects` | `list[str]` | Advisory only; never the sole cause of a failed verdict. |

## Finding *(the repair brief)*

Shape-compatible with `scanner_findings` so the existing dedup key
`(file, line, category)` and merge path apply unchanged (R6).

| Field | Type | Notes |
|---|---|---|
| `file` | `str \| None` | Implicated path where derivable. |
| `line` | `int \| None` | Where derivable. |
| `category` | `str` | e.g. `unmet_criterion`, `unexpected_regression`, `misplaced_implementation`. |
| `severity` | `"critical" \| "high" \| "medium" \| "low"` | Same vocabulary as scanner findings. |
| `criterion` | `str` | Which acceptance criterion this concerns. |
| `plan_check_id` | `str \| None` | Which step failed. |
| `expected` | `str` | |
| `observed` | `str` | |
| `evidence` | `ExecutedCheck \| None` | Command, exit code, excerpt (FR-013). |
| `repro_command` | `str \| None` | How to reproduce. |

**Invariant**: a `Finding` never carries a prescribed fix (FR-014).

## ScenarioFixture *(eval only)*

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | `healthy`, `regression`, `misplaced`, `incomplete`, `cosmetic_noop`, `env_broken`. |
| `base_files` | `dict[str, str]` | Path → content for the base commit. |
| `head_files` | `dict[str, str]` | Path → content for the head commit. |
| `criteria` | `list[str]` | Acceptance criteria handed to QA. |
| `expected_verdict` | closed enum | The verdict *class* asserted (FR-019). |
| `must_name` | `list[str]` | Substrings the failure must reference — the "right reason" assertion. |

Generated into a temp directory at eval time; never committed as a repository (R8).
