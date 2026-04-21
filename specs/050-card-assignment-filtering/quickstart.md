# Quickstart: Card Assignment Filtering

## Scenario 1 — Only coordinare-assigned cards are dispatched

### Setup
1. In `config.yaml`, add: `assignee_filter: coordinare-bot`
2. Create two cards in the TODO column:
   - Card A: assigned to `coordinare-bot`
   - Card B: assigned to `human-engineer` (or unassigned)
3. Start coordinare

### Expected
- Coordinare picks up Card A and dispatches the implementer
- Card B is left in TODO, untouched
- Dashboard idle state shows "Filter: coordinare-bot"
- Logs contain `check_board.assignee_filtered` with `skipped: 1`

### Verify
- `git log` on the created branch shows the commit is attributed to the bot identity
- Card B remains in TODO after multiple poll cycles

---

## Scenario 2 — No filter set (default behavior preserved)

### Setup
1. Remove `assignee_filter` from `config.yaml` (or leave it absent)
2. Create two cards in TODO with different assignees

### Expected
- Coordinare picks up the first (highest-priority) TODO card regardless of assignee
- No filter indicator on dashboard
- Behavior identical to pre-050 coordinare

---

## Scenario 3 — All TODO cards filtered (empty board state)

### Setup
1. Set `assignee_filter: coordinare-bot`
2. Create one TODO card assigned to `human-engineer`

### Expected
- Coordinare finds no eligible cards after assignee filtering
- Logs `check_board.assignee_filtered` with `skipped: 1`
- Phase remains `idle` — no dispatch
- Dashboard shows "No active performers · Filter: coordinare-bot"

---

## Scenario 4 — Env var override

### Setup
1. Set env: `COORDINARE_ASSIGNEE_FILTER=my-bot`
2. No `assignee_filter` in `config.yaml`

### Expected
- Filter applied using `my-bot` from env var
- Takes precedence over missing config file entry (pydantic-settings env override behavior)
