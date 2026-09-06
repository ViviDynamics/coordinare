# Dispatch Payload Additions: Assessor Workflow

Add the following row to `specs/contracts/dispatch-payload.md` in the table of fields by stage/reader.

## Assessment field (architecting stage only)

| Field Name | Type | Bounds | Stages | Description |
| --- | --- | --- | --- | --- |
| `assessment` | object or absent | contracts/assessment.schema.json | architecting only | Structured product reading from the assessing stage: goal, expected behaviour, out-of-scope items, questions, assumptions, criteria with their source, and carried clarifications. Injected only when the prior assessing stage reported `assessment_complete` with a valid assessment. Absent for all other stages and for the architecting stage when no assessment was computed. When present, the architect's intake (spec 165 architect/intake.py) renders it as the first section the architect reads. |

### Notes

- **Injection point**: dispatch_performer.inject_assessment() (parallel to inject_briefs)
- **Persistence**: PersistedSession.assessment (schema v18, backward compatible)
- **Reset rule**: Re-dispatching the assessing stage clears any prior assessment
- **Contract test**: test_dispatch_payload.py verifies assessment is present in architecting dispatch and absent from all other stages, whether the workflow is enabled or not
