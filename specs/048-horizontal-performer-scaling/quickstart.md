# Quickstart: Horizontal Performer Scaling

## Scenario 1 — Two implementers run concurrently (P1)

### Setup
1. Set `implementer.max_concurrency: 2` in config.yaml
2. Set `max_concurrent_cards: 3`
3. Create three issues on the board in TODO

### Expected behavior
1. Coordinare picks up all three cards
2. Two implementer performers launch simultaneously (slots 0 and 1)
3. Third card waits at `dispatching` phase
4. When first implementer completes → slot freed → third card dispatches

### Verify
- Dashboard: implementer shows "2 / 2 active, 1 queued"
- Coordinare log: two concurrent `dispatch_performer` events for implementing stage
- After first completion: third card dispatches within one poll cycle

---

## Scenario 2 — Assessor remains singleton (P2)

### Setup
1. Set `assessor.max_concurrency: 5` in config.yaml (attempt to override)
2. Three cards in TODO

### Expected behavior
1. Coordinare logs warning: "assessor max_concurrency clamped to 1 (singleton role)"
2. Cards assessed one at a time, not in parallel
3. Dashboard: assessor shows "1 / 1 active"

### Verify
- Log: `slot_manager.singleton_clamped` warning for assessor
- Only one assessor session active at any time

---

## Scenario 3 — Config hot-reload reduces concurrency (edge case)

### Setup
1. `implementer.max_concurrency: 3`, three implementers running
2. Operator changes config to `implementer.max_concurrency: 1` (hot-reload)

### Expected behavior
1. All three running implementers continue to completion
2. No new implementer dispatches until active count drops below 1
3. Dashboard shows "3 / 1 active" (over limit, draining)

### Verify
- No performer killed
- New dispatches blocked until active ≤ max
- Dashboard correctly shows the over-limit state

---

## Scenario 4 — Dashboard utilization view (P3)

### Setup
1. Multiple roles with different max_concurrency values
2. At least one role at capacity with a card queued

### Expected behavior
- Dashboard shows a per-role table:
  - implementer: 2 / 3 active
  - reviewer: 1 / 1 active, 1 queued
  - security: 0 / 2 active
  - assessor: 0 / 1 active (singleton)

### Verify
- All roles listed with correct active/max/queued counts
- Roles at capacity show queued count

---

## Scenario 5 — max_concurrency: 0 disables role (edge case)

### Setup
1. Set `security.max_concurrency: 0` in config.yaml
2. Card reaches security stage

### Expected behavior
1. Security role treated as disabled (equivalent to not listing it)
2. Card skips security stage via _advance_stage
3. No security performer dispatched

### Verify
- Coordinare log: `dispatch_performer.no_service_for_stage` for security
- Card advances past security to next role
