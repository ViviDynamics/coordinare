# Tasks: AI Feedback Classification

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: US1 — AI-Powered Classification (P1)

- [X] T001 [US1] Add `CLASSIFICATION_PROMPT` constant and `_classify_with_ai()` async function to `src/coordinare/graph/nodes/classify_human_feedback.py`: calls assessment backend with prompt, parses JSON response, filters by confidence >= 0.6, returns sorted concern list or None
- [X] T002 [US1] Modify `classify_human_feedback()` to try AI classification first via assessment_backend from state; fall back to keyword classification when AI returns None
- [X] T003 [US1] Log classification_method (ai/keyword) on every routing decision
- [X] T004 [P] [US1] Write unit tests: AI returns valid classification → used; AI returns low confidence → keyword fallback; AI backend error → keyword fallback

---

## Phase 2: Polish

- [X] T005 Run full test suite and linter
- [X] T006 Verify keyword tests still pass (backward compat)
