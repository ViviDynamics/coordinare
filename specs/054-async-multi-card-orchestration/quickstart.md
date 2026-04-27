# Quickstart: Async Multi-Card Orchestration Eligibility

## Scenario 1 - Eligible sessions run concurrently

### Setup
1. Set `max_concurrent_cards: 3` in `config.yaml`.
2. Create 3 TODO cards with no dependency relationships.
3. Start coordinare and wait for pickup.

### Expected
- All 3 cards are active in one cycle.
- Session ticks happen concurrently rather than one-at-a-time.

### Verify
- Daemon logs show concurrent session invocation events for all three card IDs in the same cycle window.

---

## Scenario 2 - BLOCKED/dependency sessions are skipped

### Setup
1. Keep one active card in BLOCKED column.
2. Keep one active card with unresolved `Depends on #N`.
3. Keep one independent active card eligible.

### Expected
- BLOCKED card is skipped.
- dependency-blocked card is skipped.
- eligible card proceeds normally.

### Verify
- `session_skip_reasons` contains entries for both skipped cards.
- performer dispatch/status calls occur only for the eligible card.

---

## Scenario 3 - Dependency unblocks and session resumes

### Setup
1. Have card B dependency-blocked on card A.
2. Move card A to DONE.

### Expected
- Card B becomes eligible automatically.
- Card B resumes within next poll cycles.

### Verify
- Skip reason for B disappears.
- B session enters active invocation path.

---

## Scenario 4 - Merge into main triggers rebase dispatch for in-flight branches

### Setup
1. Keep at least 2 active sessions with open PR branches.
2. Merge any other PR into `main` (squash/merge path).

### Expected
- Coordinare detects updated main SHA.
- Rebase handling is dispatched for eligible in-flight open PR branches.

### Verify
- Rebase round logs show targeted session branches.
- Branches without conflicts continue; conflicted branches are explicitly marked.

---

## Scenario 5 - QA freshness gate catches behind-main branches

### Setup
1. Pick an in-flight branch intentionally behind latest `main`.
2. Trigger QA performer on that branch.

### Expected
- QA reports branch freshness criterion failure (or explicit blocker) and does not silently pass.

### Verify
- QA output includes freshness payload/evidence.
- After rebase onto latest main, rerun QA and confirm freshness criterion passes.
