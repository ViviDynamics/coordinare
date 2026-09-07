# Dispatch Payload Additions: Spec 170 (Security Workflow)

To be applied to `specs/contracts/dispatch-payload.md`.

## Changed rows

| Field | Change |
| --- | --- |
| `pr_diff` | The producer note gains: injected for the `security` stage when the security role's configured workflow is `security` (dispatch skips the spec-083 floor and injects the sanitized diff instead). Without the workflow the security stage still receives no `pr_diff`. |
| `scanner_findings` | The producer note gains: `[]` when the security role runs the workflow (the scan happens inside the performer). |
| `review_findings` | The producer note gains a second producer: `monitor_performer` on `security_failed` from the security stage when the findings route to the implementer, lifting the blocking findings in the same record shape. Still injected into the implementing stage only. |

## No new fields

`Score` is unchanged. The security workflow reads `pr_diff`, `implementation_brief`, `pr_url` and `workflow_env`, all registered.
