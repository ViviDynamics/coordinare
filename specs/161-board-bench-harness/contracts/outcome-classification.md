# Contract: Dispatch Outcome Classification

**Owner**: `src/coordinare/bench/harness_outcome.py` (spec 161, FR-001 through FR-006)

## Input

One `PersonaDispatch` from a `RunArtifact` (spec 134, schema v1).

## The one hard invariant

Classification reads **`terminal_marker`**. It MUST NOT read `status`.

`runner.py::_dispatch_status` collapses every non-success marker into `"failed"`, mixing
legitimate negative role verdicts with genuine harness defects. Any implementation that
consults `status` is wrong, and the test suite pins this: a dispatch with
`status="failed"` and `terminal_marker="changes_requested"` MUST classify as `CREDIT`.

## Mapping

| Input `terminal_marker` | Output class |
| --- | --- |
| member of `TERMINAL_SUCCESS_STATES` (imported from `coordinare.graph.nodes.monitor_performer`) | `CREDIT` |
| `changes_requested` | `CREDIT` |
| `qa_failed` | `CREDIT` |
| `security_failed` | `CREDIT` |
| `blocked` | `CREDIT` |
| `malformed_output` | `HARNESS_DEFECT` |
| `system_error` | `HARNESS_DEFECT` |
| `env_blocked` | `ENVIRONMENT` |
| `None` or `""` | `INCONCLUSIVE` |
| any other string | `INCONCLUSIVE`, and the marker is recorded in `unknown_markers` |

## Guarantees

1. **Total**: every input maps to exactly one class. No input raises.
2. **Credit set is imported, not copied**: adding a new member to
   `TERMINAL_SUCCESS_STATES` upstream automatically credits it here. A test asserts the
   function's credit set equals the imported constant plus the four negative verdicts, so
   a copied literal would fail.
3. **Unknown is surfaced, never silent**: an unrecognized marker is `INCONCLUSIVE` and is
   reported. It is never credited and never counted as a defect.
4. **Negative verdict is not a defect**: this is the feature's whole point. A role that
   correctly says no is doing its job.

## Non-goals

- Judging whether a negative verdict was *correct*. That is card-level grading
  (`grader.py`), which this contract does not touch.
- Changing `_dispatch_status` or `grader.py`. Both keep their current behavior.
