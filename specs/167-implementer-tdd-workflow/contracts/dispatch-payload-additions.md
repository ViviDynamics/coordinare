# Dispatch Payload Additions: Implementer Workflow

No new fields are added to the dispatch payload for the implementer workflow.

## Rationale

The implementer workflow operates entirely within the performer container and does not require additional coordinare-provided context beyond what is already present:

- **implementation_brief**: already provided by spec 165 (architect blueprint)
- **local_test_gate**: already provided by spec 089 (implementer local test gate)
- **model, max_tokens, persona**: already part of Score for all roles

The workflow receives the milestone plan from the brief (if present), or derives one from the card's acceptance criteria if the brief is absent or marks the card as single-turn (FR-003).

Quality commands are declared via `score.workflow_env["QUALITY_COMMANDS"]` (newline-separated), which is already a supported coordinare mechanism for passing role-specific configuration (used by 093, 165, 166, and others).

The run record (turn history, final status, metrics) is produced by the workflow and travels in `PerformerResponse.report`, not in the payload. It is not consumed by subsequent roles and does not appear in the dispatch payload.

## Confirmation

The implementer role's dispatch payload remains unchanged when `workflow: implementer` is enabled. Existing roles (architect, qa, reviewer, etc.) are unaffected. The performer remains wire-compatible with coordinare: the contract between performer and coordinare (PerformerResponse shape) is unchanged.
