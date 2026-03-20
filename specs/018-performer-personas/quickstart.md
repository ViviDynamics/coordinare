# Quickstart: Performer Personas

**Branch**: `018-performer-personas` | **Date**: 2026-03-18

This guide shows how to configure, verify, and test the performer personas feature locally.

## Prerequisites

- Coordinare daemon running (`python -m coordinare` or equivalent)
- `config.yaml` present and valid
- Dashboard accessible at `http://localhost:8090`

---

## Scenario 1 — Configure an implementer persona via config.yaml

**Goal**: Verify that a custom implementer persona is injected into the next dispatch payload.

**Steps**:

1. Open `config.yaml` and add:
   ```yaml
   personas:
     implementer:
       instructions: |
         Always write tests first using TDD (Red-Green-Refactor).
         Prefer pure functions over stateful classes.
         Add structured logging at DEBUG level for all non-trivial paths.
   ```

2. Trigger a card dispatch (move a card to IN_PROGRESS on your GitHub board, or use the force-poll button on the dashboard).

3. Inspect the performer's stdin log (or enable DEBUG logging) — verify the dispatch payload contains:
   ```json
   {
     "action": "dispatch",
     "payload": {
       "title": "...",
       "persona_instructions": "Always write tests first using TDD...",
       ...
     }
   }
   ```

4. **Expected**: `persona_instructions` field is present and matches `config.yaml`.

---

## Scenario 2 — Verify default persona when no config is set

**Goal**: Confirm that removing the `personas:` section from `config.yaml` restores built-in default behavior.

**Steps**:

1. Remove the `personas:` section from `config.yaml` (or leave it absent entirely).
2. Trigger a dispatch as above.
3. Inspect the performer stdin log — verify `persona_instructions` contains the built-in implementer default (not empty, not custom text).

**Expected**: `persona_instructions` is the built-in default implementer instructions.

---

## Scenario 3 — Configure and verify assessor persona

**Goal**: Confirm assessor persona is injected into the assessment prompt.

**Steps**:

1. In `config.yaml`:
   ```yaml
   personas:
     assessor:
       instructions: |
         Focus on business value and user impact.
         Only ask clarifying questions when acceptance criteria are genuinely ambiguous.
         Do not ask about implementation details — only about user-facing behavior.
   ```

2. Trigger an assess cycle (move a card through the advocate or directly to TODO).

3. With DEBUG logging enabled, look for a log entry from `assess_card` showing the prompt. Verify the prompt starts with:
   ```
   ## Assessor Instructions
   Focus on business value and user impact...
   ```

**Expected**: Assessor persona instructions appear at the top of the assessment prompt.

---

## Scenario 4 — Edit persona via dashboard

**Goal**: Verify the dashboard API round-trip without editing `config.yaml` manually.

**Steps**:

1. Open `http://localhost:8090` and navigate to the Personas section.
2. Find the **Implementer** row — note current instructions (or "Using default instructions").
3. Click Edit, enter new instructions, click Save.
4. Verify the UI shows the new instructions immediately.
5. Trigger a dispatch — verify the payload contains the new instructions.

**Expected**: Dashboard update takes effect within one poll cycle (≤ 30 seconds); no daemon restart required.

---

## Scenario 5 — Reset persona to defaults via dashboard

**Goal**: Verify the Reset action restores built-in defaults.

**Steps**:

1. Ensure a custom implementer persona is set (from Scenario 4 or config.yaml).
2. On the dashboard Personas section, click **Reset to defaults** for the Implementer row.
3. Verify the UI shows "Using default instructions".
4. Trigger a dispatch — verify `persona_instructions` contains the built-in default.

**Expected**: Custom instructions are cleared; built-in default is active immediately.

---

## Scenario 6 — Verify length limit enforcement

**Goal**: Confirm that instructions exceeding 8,000 characters are rejected.

**Steps (API)**:

```bash
# Generate a 8001-character string
LONG=$(python3 -c "print('x' * 8001)")

curl -s -X PUT http://localhost:8090/api/personas/implementer \
  -H "Content-Type: application/json" \
  -d "{\"instructions\": \"$LONG\"}" | jq .
```

**Expected response**:
```json
{
  "error": "Instructions exceed maximum length (8000 chars)"
}
```
HTTP status: `422` or `400`.

---

## API Quick Reference

```bash
# List all personas
curl http://localhost:8090/api/personas | jq .

# Update implementer persona
curl -X PUT http://localhost:8090/api/personas/implementer \
  -H "Content-Type: application/json" \
  -d '{"instructions": "Write tests first."}' | jq .

# Reset assessor to defaults
curl -X DELETE http://localhost:8090/api/personas/assessor
# → HTTP 204 No Content
```
