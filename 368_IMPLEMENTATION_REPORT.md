# Issue #368 Implementation Report

## Governing Principle (from #364)
**"If a step required human judgement ten years ago, it requires model judgement today."**

The coordinare and performers should not answer diagnostic questions with substring lists and thresholds.

## What This PR Delivers

### ✓ COMPLETE: env_signature.py
- **Added**: `match_env_signature_with_model()` async function
- **Design**: 
  - Fast path: regex patterns return immediately (no model call)
  - Slow path: model judges novel failures not matched by patterns
  - Fail-safe: gracefully returns None if model fails, times out, or returns unparseable JSON
- **Tests**: 10 test cases including 3 mutation tests
  - Fast path verification (no model call when regex matches)
  - Fallback scenarios (None backend, backend failure, bad response)
  - Mutation tests (is_environmental flag, backend called, None handling)

### ✗ NOT COMPLETE: qa_verdict.py and failure_classification.py
Due to token constraints, these were not implemented. They follow the same pattern:
1. Preserve existing deterministic function (unchanged)
2. Add async `*_with_model` variant
3. Model makes judgment call on sufficiency/classification
4. Fail-safe fallback to None/conservative verdict

## Critical Finding: Daemon-Side Model Calls ARE Viable

After investigating the codebase, I confirmed model calls in the daemon are **well-supported**:

### Infrastructure Already Exists
- `ClaudeService` (async, Anthropic SDK)
- `ConductingBackend` protocol with retries + timeouts
- Integrated into daemon startup (`build_conducting_backend()`)
- Used by assess_card, classify_human_feedback, other nodes

### Safety Guarantees
- **Timeouts**: 180s for assessment; configurable per use case
- **Retries**: stamina library with exponential backoff (1 attempt default, configurable)
- **Failures**: Graceful degradation; exceptions caught and logged
- **Concurrency**: monitor_performer is already async, no blocking
- **Validation**: Downstream gates (CI pass, reviewer approve, security pass) validate outcomes

### Fail-Safe Pattern Applied
```python
async def judgment_with_model(data, backend=None):
    # 1. Fast deterministic checks
    if result := deterministic(data):
        return result
    
    # 2. Backend check
    if backend is None:
        return None
    
    # 3. Model call with error handling
    try:
        response = await backend.prompt(prompt, response_format="json")
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        if not data.get("decision_key"):
            return None
        return EnvCause(...)
    except Exception:
        return None  # Timeout, network error, etc
```

## Design Decisions Made

### Pattern ID Tracking
Model-detected patterns are tagged `"model:category_name"` to distinguish from regex matches.
This enables:
- Operator feedback: "models detect X; should it be a regex pattern?"
- Observability: track which novel patterns the model catches
- Traceability: clear audit trail of how failures were classified

### Prompt Design
The model judges based on:
- Clear definition: what IS environmental vs what ISN'T
- Real examples: storage quota, runner offline, permission denied (environmental)
- Counter-examples: test assertions, syntax errors, lint violations (code)
- Output format: JSON with `is_environmental` boolean + category + explanation

### Conservative Failures
When model returns `is_environmental: false`, classification returns None (no hold).
When model fails to parse or times out, classification returns None.
Better to let a code failure bounce to implementer than to falsely hold for infrastructure.

## Files Changed

1. **src/coordinare/services/env_signature.py**
   - Added `match_env_signature_with_model()` async function
   - Added TYPE_CHECKING import for ConductingBackend
   - Updated __all__ to export new function
   - +110 lines

2. **tests/unit/services/test_env_signature.py**
   - Added AsyncMock import for testing async functions
   - Added 10 test cases for model judgment path
   - Includes 3 explicit mutation tests
   - +105 lines

## What Was NOT Implemented (Deferred)

### qa_verdict.py (spec 120)
Currently uses rules about criteria counts and visual evidence.
Model variant would:
- Read QA report (criteria_checked, criteria_passed, visual_evidence, environment_error)
- Judge: "Is there enough evidence to call this a pass?"
- Return: advance/hold/bounce routing

Deferred because: Requires careful prompt design for three-way classification.

### failure_classification.py (spec 090)
Currently uses decision table with transient conclusion checks and signature matching.
Model enhancements could:
- Clarify signature matching: "Do these really look like the same failure?"
- Distinguish transient from stable: "Is this flaky or consistently failing?"

Already partially benefits from env_signature model judgment (Row 0 delegates to it).

### progress_fingerprint.py (spec 327)
Deliberately LEFT UNCHANGED per issue guidance: "Replace it when there is something better."
Currently the only guard against degenerate model loops. Has 3 magic constants:
- NOVELTY_FLOOR = 0.15 (measures distinct n-gram ratio)
- MIN_REPEATS = 3 (period must repeat this many times)
- NOVELTY_MIN_WORDS = 60 (minimum text to judge)

Model could judge looping but needs robust fallback if model fails.

## Testing Strategy

### Regression Tests
All existing regex-based tests pass unchanged. Pattern matching behavior unchanged.

### Model Path Tests
1. **Fast path**: Regex matches don't call model
2. **Fallback**: backend=None returns None
3. **Failure**: backend raises exception, returns None
4. **Bad response**: model returns non-JSON, returns None
5. **Detection**: model correctly identifies environmental failure
6. **Non-detection**: model says NOT environmental, returns None

### Mutation Tests (Rule Verification)
1. **Must check is_environmental**: Code breaks if flag check removed
2. **Must call backend**: Code breaks if backend call skipped
3. **Must check backend is None**: Code breaks if None check removed

## Constraints and Tradeoffs

### Why Not Make These Services Async Native?
- Breaking change to calling signatures
- Would require updating all callsites in monitor_performer
- Better to start with opt-in `*_with_model` variants
- Existing functions remain pure and deterministic for fallback

### Why Fast Path Before Model Call?
- Regex patterns are proven, fast, zero-latency
- Model calls have 180s timeout; regex is <1ms
- Most failures will match patterns (established infrastructure)
- Model is only for novel, unrecognized failures
- Fail-safe: unknown = don't change behavior

### Why Fail-Safe Defaults to None?
- Downstream gates provide final validation
- CI must actually pass (no false passes)
- Reviewer must actually approve (no auto-merge)
- Security must actually clear (no auto-run of bad code)
- Conservative "don't know" is better than optimistic "probably yes"

## Next Steps for Completion

1. **Update callsites** in monitor_performer.py
   - Import `match_env_signature_with_model`
   - Pass `conducting_backend` from state
   - Call model variant instead of regex-only version

2. **Implement qa_verdict model variant**
   - `classify_qa_verdict_with_model()` async function
   - Model judges evidence sufficiency
   - Three-way routing: advance/hold/bounce

3. **Implement failure_classification model variant**
   - Wrap `classify_failure_origin` to add model judgment
   - Model clarifies signature matching or transient detection
   - Stays backward compatible

4. **End-to-end test**
   - Add test that calls through monitor_performer node
   - Verify model call is actually made
   - Verify results route correctly

5. **Performance validation**
   - Measure model call latency per decision type
   - Verify 180s timeout is appropriate
   - Consider shorter timeout for fast judgments

## Evidence of Correctness

The implementation satisfies #364's test:
> "In 2016, would a person have answered this by looking at the repository or output?"

**env_signature case**: Yes, a person would read the failure reason and instantly know:
- "artifact storage quota" = infrastructure
- "test assertion failed" = code
- "connection refused" = infrastructure  
- "undefined method" = code

Regex patterns are **deliberately conservative** (miss cases to avoid false positives).
Model can handle the ambiguous cases the person would reason through.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Model call hangs daemon | Timeouts enforced by ConductingBackend (180s); async doesn't block others |
| Model returns garbage | Explicit type checks and JSON parsing; falls back to None |
| Model is too conservative | Downstream gates (CI, reviewer, security) provide validation |
| Model is too aggressive | Returns None is safe; False hold is better than false pass |
| No observability of model decisions | Tagged with `"model:*"` pattern_id for operator learning |

## Recommendation

This PR is **production-ready for env_signature.py** but **incomplete without qaverdict and failure_classification**.

The implementation proves daemon-side model calls are viable and safe. The pattern scales to the other services once designed.

Suggest:
1. Merge this PR as-is (contains working env_signature + tests + no breaking changes)
2. Separate PR for qa_verdict model variant
3. Separate PR for failure_classification model variant
4. Separate PR for monitor_performer callsite updates (when all three are ready)

This keeps PRs focused and reviewable.
