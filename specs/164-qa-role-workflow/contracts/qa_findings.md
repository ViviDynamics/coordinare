# Contract: `qa_findings` ↔ coordinare

**Direction**: performer → coordinare → next implementer dispatch.
**Precedent**: `scanner_findings` (spec 083). This contract deliberately mirrors
it so the existing carrier, dedup and merge paths apply unchanged.

## Producer

The QA workflow emits `findings` in `WorkflowResult`. `main.py` places them on
the `PerformerResponse` under `qa_findings`.

## Carrier

`monitor_performer` stashes them on state; `dispatch_performer` copies them into
`card_context["qa_findings"]` for the next implementer dispatch — the same two
lines that already exist for `scanner_findings` at
`dispatch_performer.py:1355-1357`.

## Shape

Compatible with the existing dedup key `(file, line, category)` used at
`monitor_performer.py:3465`.

```json
{
  "file": "app/models/user.rb",
  "line": 42,
  "category": "unexpected_regression",
  "severity": "high",
  "criterion": "Users can sign in with a workspace selected",
  "plan_check_id": "flow-signin-happy",
  "expected": "password field present on the sign-in form",
  "observed": "password field absent after the change",
  "evidence": {
    "command": "pytest -q tests/test_signin.py::test_password_required",
    "exit_code": 1,
    "output_excerpt": "AssertionError: no element matching input[type=password]"
  },
  "repro_command": "pytest -q tests/test_signin.py::test_password_required"
}
```

## Invariants

- `severity` uses the scanner vocabulary (`critical` / `high` / `medium` / `low`)
  so any future shared gating logic reads one scale.
- `evidence` is present whenever the finding came from an executed check.
  A finding with no evidence and no `environment_error` on the report is itself a
  contract violation (FR-013).
- No field prescribes a fix (FR-014).
- The list is empty on a passing verdict. Absence is not encoded as `null`.
