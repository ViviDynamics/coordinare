# Research: Rank Backend Harnesses Per Role (161)

All findings below were obtained by **executing** against the real tree, not by reading
and inferring. Where a claim was checked by running code, the observed output is quoted.

## R1: Does the sweep override path already reach a role's harness?

**Decision**: Yes. The US2 sweep dimension is a declaration plus validation, not new
override machinery.

**Evidence** (executed against `benchmarks/spaces/baseline.yaml`):

```
resolve global_config.performers.reviewer.backend -> opencode
materialize({'global_config.performers.reviewer.backend': 'junie'}) -> reviewer.backend = junie
```

`space.py::_walk` does generic dotted-path navigation over the baseline dump and
`materialize` re-validates the result against `CoordinareConfiguration`. `backend` is an
ordinary field on the role, so it resolves like `mode` does.

**Rationale**: This collapses most of the anticipated US2 work. The plan reflects the
smaller real scope rather than the assumed one.

**Alternatives considered**: Adding a bespoke harness-override mechanism. Rejected: it
would duplicate a working generic path, violating the "no parallel stack" constraint.

## R2: Does anything reject an invalid harness name?

**Decision**: No, and this is the real work behind FR-017.

**Evidence** (executed):

```
materialize({'global_config.performers.reviewer.backend': 'not_a_harness'})
  -> INVALID harness ACCEPTED (schema does not constrain backend)
materialize({'global_config.performers.nosuchrole.backend': 'junie'})
  -> SpaceError: path 'global_config.performers.nosuchrole.backend':
     segment 'nosuchrole' does not resolve in the baseline
```

So the **role** half of FR-017 already holds (rejected at path-walk time), but the
**harness-name** half does not: a typo would expand into a sweep point, run, and fail
mysteriously partway through a long sweep.

**Rationale**: Validate the harness name against the known harness set at space-load
time, matching how `space.py` already fails loudly on unresolvable paths.

**Alternatives considered**: Constraining `backend` in the main config schema. Rejected
for this feature: it changes validation for the whole product surface, well beyond a
benchmark change, and risks rejecting configs that work today. Noted as a possible
follow-up rather than silently expanded into.

## R3: Where does harness quality actually surface in the artifact?

**Decision**: In `PersonaDispatch.terminal_marker`. Classification MUST read the marker
and MUST NOT read the collapsed `status`.

**Evidence** (read from `runner.py::_dispatch_status`):

```python
if marker in TERMINAL_SUCCESS_STATES or marker in {"succeeded", "ok"}: return "succeeded"
if marker in {"error", "cancelled"}: return marker
if not marker: return "cancelled"
return "failed"  # changes_requested, qa_failed, security_failed, blocked, ...
```

Every non-success marker collapses to `"failed"`. That single bucket contains both a
reviewer correctly rejecting a bad pull request (`changes_requested`) and a harness that
could not produce usable output (`malformed_output`). The comment in the source names
this explicitly.

`grader.py` is likewise unsuitable as the signal: it decides `FAIL_HARNESS` from
`outcome.final_state == "error" or not outcome.dispatches`, a coarse card-level
infrastructure check, not a per-dispatch quality measure.

**Rationale**: The raw marker is retained on the artifact, so the distinction is
recoverable from **already-captured** runs. This is what makes US1 shippable without a
healthy inference host.

**Alternatives considered**: Adding a new per-dispatch quality field to the artifact.
Rejected: it would require re-running every board to get any data, and the needed
information is already present.

## R4: What is the authoritative marker vocabulary?

**Decision**: Import `TERMINAL_SUCCESS_STATES` from
`coordinare.graph.nodes.monitor_performer` as the credit set. Do not duplicate the literal.

**Evidence** (read from source):

```python
TERMINAL_SUCCESS_STATES = frozenset({
    "pr_opened", "plan_committed", "approved", "security_passed",
    "qa_passed", "docs_committed", "assessment_complete",
})
```

Other markers observed in the same module: `changes_requested`, `qa_failed`,
`security_failed`, `blocked` (negative verdicts); `malformed_output`, `system_error`
(harness defects); `env_blocked` (environment).

**Rationale**: A duplicated literal would drift the moment a new success marker is added,
and would silently reclassify a success as inconclusive. Importing the constant means a
new success state is picked up automatically.

**Alternatives considered**: Hardcoding the set in the new module for isolation. Rejected:
drift risk outweighs the coupling, and the coupling is to a stable domain constant.

## R5: How should ties be decided without inventing statistics?

**Decision**: Reuse `noise.py::component_stats()`. Two candidates tie when
`|mean_a - mean_b| <= stdev_a + stdev_b`.

**Evidence**: `component_stats(values)` returns
`ComponentStats(mean, variance, stdev, min, max, n)`, and `noise.py` already owns the
repeat-and-aggregate machinery (`run_repeats`, `aggregate`) including first-class handling
of failed repeats.

**Rationale**: The repo already has an agreed notion of run-to-run spread. Defining a new
significance test would be a second, competing idiom. With a single repeat `stdev` is 0,
so only exactly equal scores tie, which is honest rather than falsely precise.

**Alternatives considered**: Confidence intervals or a t-test. Rejected as
disproportionate: sample counts here are small single digits, and the goal is to avoid
declaring a winner on noise, not to publish a p-value.

## R6: What shape should the harness objective take?

**Decision**: Mirror `score.py::compute_scalar` — a credit term less normalized time and
token penalties, with weights embedded in the emitted result.

**Evidence**: the existing shape is
`w_correctness*correctness_rate - w_cost*(cost/cost_budget) - w_time*(time/time_budget)`,
and `ScoreObject` embeds `Weights` so that "two scalars are comparable only when their
weights match".

**Rationale**: Structural symmetry makes the two views legible side by side and reuses the
established "embed the weights" discipline. The only substitution is the credit term:
credited dispatches over conclusive dispatches, instead of card correctness.

**Alternatives considered**: A bespoke composite (for example defect rate weighted against
p95 latency). Rejected: a second scoring idiom for no gain, and it would not inherit the
weights-fingerprint guard.

## R7: Where do the tests belong?

**Decision**: `tests/unit/test_161_*.py`.

**Evidence**: the established pattern is `test_<NNN>_<topic>.py` under `tests/unit/`
(`test_134_artifact_schema.py`, `test_135_grader.py`, `test_136_space.py`,
`test_137_optimizer.py`, `test_151_runner_failure.py`). `tests/benchmarks/` contains only
`test_performer_perf.py` and is a performance home, not a benchmark-feature home.

**Rationale**: Follow the convention actually in use rather than the one the directory
name suggests.

## R8: Is there an importable list of valid harness names?

**Decision**: No. Extract one. FR-017 validation needs a single source of truth, and
today there is none.

**Evidence** (executed, then read):

```
from performer.backends import SUPPORTED_BACKENDS
  -> ImportError: cannot import name 'SUPPORTED_BACKENDS' from 'performer.backends'
```

The module imports fine from the coordinare venv, but the name does not exist. Reading the
source, the harness set is a **function-local dict** declared inside `get_backend()`:

```python
def get_backend(name: str) -> "BackendAdapter":
    """Raises UnsupportedBackendError if *name* is not in SUPPORTED_BACKENDS."""
    supported_backends: dict[str, tuple[str, str]] = {
        "opencode": ..., "opencode_compat": ..., "junie": ...,
        "claude_code": ..., "codex": ..., "hermes": ..., "pi": ..., "openclaw": ...,
    }
```

So the docstring names a constant that is not defined anywhere. The eight valid harness
names are reachable only by calling the factory.

Separately confirmed: `backend` is **not** typed as a constrained literal in
`src/coordinare/models/config.py` (no match), which is the root of R2.

**Decision detail**: promote the dict to a module-level `SUPPORTED_BACKENDS` constant and
have `get_backend()` read it. This is a three-line refactor that makes the existing
docstring true and gives both the factory and the new space validation one source of
truth.

**Rationale**: The alternatives are worse. Duplicating the eight names inside
`coordinare.bench` guarantees drift the first time a backend is added, and the repo's
standing guidance is to prefer a durable shared contract over a copied literal. Probing
via `get_backend(name)` is unsuitable for validation because it imports the adapter class
and its optional dependencies, which the source comment explicitly warns can break
unrelated configurations.

**Scope note**: this touches the `performer` package rather than `coordinare.bench`. It is
in scope because FR-017 cannot be implemented correctly without it, and it is additive
(no behavior change to `get_backend`, which keeps raising `UnsupportedBackendError` for
the same inputs). A regression test pins that equivalence.

**Alternatives considered**: Constraining `backend` in the coordinare config schema
(rejected in R2: product-wide validation change, out of proportion to this feature).
