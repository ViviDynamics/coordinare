# Implementation Plan: AI Feedback Classification

**Branch**: `029-ai-feedback-classification` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Replace the keyword-only PR comment classifier in `classify_human_feedback.py` with an AI-powered classifier that calls the existing assessment backend. The keyword heuristic is retained as a fallback when the backend is unavailable. One file is modified; one new test file covers the AI path. No new dependencies.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (existing), pydantic (existing) -- **no new dependencies required**
**Storage**: N/A -- classification is stateless; no caching
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project -- existing `src/coordinare/` layout
**Performance Goals**: One additional async API call per review cycle; < 2s latency expected
**Constraints**: Must preserve existing keyword classifier as importable function; must not break existing tests
**Scale/Scope**: ~1 modified file, ~1 new helper function, ~10 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | AI classifier is a clean function; keyword fallback unchanged |
| II. Testing Discipline | PASS | Both AI and fallback paths independently testable via mocked backend |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | Single async call; fallback avoids blocking on timeout |
| V. Clarity Before Action | PASS | No unresolved clarifications |

## Project Structure

### Source Code (files changed)

```text
src/coordinare/graph/nodes/
└── classify_human_feedback.py        # MODIFIED -- add AI classification path + fallback logic

tests/unit/graph/nodes/
└── test_classify_human_feedback.py   # MODIFIED -- add AI classification tests (~10 new tests)
```

## Detailed Implementation Plan

### Step 1 -- Add Classification Prompt and Types (`classify_human_feedback.py`)

Add a module-level classification prompt and a TypedDict for the response:

```python
CLASSIFICATION_PROMPT = """Classify the following PR review comments into concern categories.
Valid categories: implementation, architecture, security, documentation, qa, review.
Return a JSON list of objects with "concern" and "confidence" (0.0-1.0) fields.
Only include concerns that are clearly present in the comments."""

CONFIDENCE_THRESHOLD = 0.6

class FeedbackClassification(TypedDict):
    concern: str
    confidence: float
```

---

### Step 2 -- Add AI Classification Function (`classify_human_feedback.py`)

```python
async def _classify_with_ai(
    reviews: list[dict[str, Any]],
    assessment_backend: AssessmentBackendProtocol,
) -> list[FeedbackClassification] | None:
    """Classify reviews using the assessment backend. Returns None on failure."""
    combined_text = _extract_review_text(reviews)
    try:
        result = await assessment_backend.assess({
            "prompt": CLASSIFICATION_PROMPT,
            "content": combined_text,
            "response_format": "json",
        })
        classifications = result.get("classifications", [])
        # Validate and filter by confidence threshold
        return [c for c in classifications
                if isinstance(c, dict)
                and c.get("confidence", 0) >= CONFIDENCE_THRESHOLD
                and c.get("concern") in CONCERN_TO_STAGE]
    except Exception:
        logger.warning("classify_human_feedback.ai_classification_failed", exc_info=True)
        return None
```

---

### Step 3 -- Modify Main Classification Flow (`classify_human_feedback`)

Update `classify_human_feedback` to try AI first, fall back to keywords:

```python
# In classify_human_feedback():
assessment_backend = state.get("assessment_backend")
ai_concerns = None
if assessment_backend is not None:
    ai_concerns = await _classify_with_ai(pending_reviews, assessment_backend)

if ai_concerns is not None and ai_concerns:
    concern_names = sorted({c["concern"] for c in ai_concerns})
    classification_method = "ai"
else:
    concern_names = classify_feedback_concerns(pending_reviews)
    classification_method = "keyword"

logger.info("classify_human_feedback.method", method=classification_method)
```

The rest of the routing logic (earliest stage selection, lifecycle re-entry) is unchanged.

---

### Step 4 -- Tests (`test_classify_human_feedback.py`)

Add tests for:
1. AI classification returns valid concerns -> routes to earliest stage
2. AI classification returns concerns below threshold -> falls back to keyword
3. AI classification returns empty list -> falls back to keyword
4. Assessment backend raises exception -> falls back to keyword
5. Assessment backend returns malformed response -> falls back to keyword
6. Assessment backend is None in state -> uses keyword directly
7. AI returns unknown concern category -> filters it out
8. Mixed confidence scores -> only high-confidence concerns used
9. Classification method is logged correctly for AI path
10. Classification method is logged correctly for keyword fallback path

## Complexity Tracking

No constitution violations.

| Change | Scope | Justification |
|--------|-------|---------------|
| 1 modified node file | ~40 new LOC | AI path + fallback branching; existing function untouched |
| ~10 new tests | ~120 LOC | Both classification paths and edge cases |
