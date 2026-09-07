# Run-record additions (spec 171)

Applied to `specs/167-implementer-tdd-workflow/contracts/run-record.schema.json`.
Both fields are optional with defaults, so a pre-171 record validates unchanged
and coordinare, which reads the report as a dict, needs no change.

| Field | Where | Type | Meaning |
| --- | --- | --- | --- |
| `resumed_from_milestone` | top level | `integer \| null` (default null) | The index of the first milestone this run actually ran, when earlier ones were skipped as already done by a previous run of the same card. Null when the run started at the top of the plan. |
| `satisfied_by` | each `per_milestone` entry | `"this_run" \| "prior_run" \| "existing_tests"` (default `"this_run"`) | How the milestone came to be complete. `prior_run` was skipped at plan time; `existing_tests` had a tests turn that changed nothing over tests that already cover it; `this_run` ran. |

`milestones_planned` keeps its meaning (the full plan length) and
`milestones_completed` counts skipped milestones, so a resumed run that finishes
reports `n/n` rather than the number of turns it took.

No dispatch-payload change. The resume evidence is the branch's own git history,
read inside the performer; `Score` carries nothing new.
