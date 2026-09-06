# Dispatch payload additions (spec 165)

Rows to add to `specs/contracts/dispatch-payload.md` Field Registry, each with a contract test in `tests/contract/test_dispatch_payload.py` and a declaration on `Score` (`extra="ignore"` drops undeclared fields in transit).

| Field | Type | Required | Producer | Consumer |
| --- | --- | --- | --- | --- |
| `implementation_brief` | dict | no | dispatch_performer (projection of `PersistedSession.blueprint`) | implementer prompt via `_card_docs`: summary, milestones, modules, data_model, interfaces, risks, size |
| `documentation_brief` | dict | no | dispatch_performer (projection) | documenter side run prompt: summary, docs, modules |
| `verification_brief` | dict | no | dispatch_performer (projection) | QA workflow plan step: criteria, summary |
| `implementer_single_turn` | bool | no | dispatch_performer (from `blueprint.size == "small"`) | implementer persona: omit the milestone loop and PARTIAL_PROGRESS instructions |

Absent when the card has no blueprint, so the pre-165 payload is unchanged in shape.

## Performer report (architecting role, workflow active)

`PerformerResponse.report`:

```json
{
  "blueprint": { "...": "validated Blueprint, see blueprint.schema.json" },
  "size": "small | large",
  "write_free_check": { "command": "git status --porcelain", "exit_code": 0, "passed": true, "refused_commands": 0 },
  "workflow_metrics": { "model_calls": 2, "truncation_retries": 0, "schema_reprompts": 0, "step_durations_ms": { "intake": 5, "survey": 91000, "blueprint": 180000, "size": 1, "report": 2 } }
}
```

`status` is `done` when the blueprint validated; a hollow or invalid blueprint yields `status: error` with `reason` naming the fields.
