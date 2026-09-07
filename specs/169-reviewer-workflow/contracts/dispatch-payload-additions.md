# Dispatch Payload Additions: Spec 169 (Reviewer Workflow)

To be added to `specs/contracts/dispatch-payload.md`.

## New fields in Score

### review_findings (implementing stage only)

| Field | Type | Bounds | Note |
|-------|------|--------|------|
| `review_findings` | `dict \| null` | per review-record.schema.json | Injected only for the implementing stage when a reviewer has reported changes_requested with findings. Carries the complete ReviewRecord (parsed diff, findings, dispositions, survey commands, coverage report, verdict). Null for all other stages and when no prior review findings exist. Default: None. |

## Updated field registrations

No existing fields are modified. These additions follow the pattern of spec 165 (brief injection) and spec 166 (assessment injection).

## Stage-by-stage breakdown

|-------|-----------------|-------------------|
| implementing | Injected if present | Not injected |
| reviewing | Not injected | Injected |
| architecting | Not injected | Not injected |
| qa | Not injected | Not injected |
| documenting | Not injected | Not injected |
| closing_review | Not injected | Not injected |
| assessing | Not injected | Not injected |

## Rationale

- `review_findings` is injected only to the implementing stage because only the implementer reads and repairs review findings (spec FR-012, FR-013).
- All other stages are unaffected by spec 169 and receive neither field.
