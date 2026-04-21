# Quickstart: Dashboard Redesign

## Scenario 1 — Active performers visible at a glance (P1)

### Setup
1. Start coordinare with 2 cards in TODO (e.g. issues #89 and #91)
2. Let both progress to the implementing/reviewing stage
3. Open `http://localhost:9091` in browser

### Expected
- Two tiles visible above the fold, no scrolling
- Each tile shows: role name, card title + issue number, elapsed time
- Workflow diagram below the tiles, < 25% viewport height

### Verify
- Both cards show within 3 seconds of page load
- Refreshing preserves the view (SSE reconnects)

---

## Scenario 2 — Multi-page navigation with pushState (P2)

### Setup
1. Open dashboard at `http://localhost:9091`
2. Click "Performers" in the navbar

### Expected
- Page content changes to show the /performers view
- URL updates to `http://localhost:9091/performers`
- "Performers" link highlighted in navbar
- No full page reload (SSE stream stays connected)

### Verify
- Click browser back button → returns to `/`
- Click browser forward → returns to `/performers`
- Bookmark `/personas` and open → loads personas page directly

---

## Scenario 3 — Persona editor on its own page (P3)

### Setup
1. Navigate to `http://localhost:9091/personas`
2. Edit the reviewer's instructions

### Expected
- All 8+ roles listed with current instructions
- Edit one role's instructions and save
- Navigate to `/` and back to `/personas`
- Edited instructions persisted

### Verify
- Change takes effect on next dispatch without restart (hot-reload preserved)
- No persona editor visible on the main `/` page

---

## Scenario 4 — Performers page with utilization (P4)

### Setup
1. Configure `implementer.max_concurrency: 2` in config
2. Dispatch 2 cards to implementing stage
3. Navigate to `http://localhost:9091/performers`

### Expected
- Implementer row shows "2 / 2 active" (or "active" if 048 not configured)
- Each active card title visible in the row
- Session elapsed time shown

### Verify
- Idle roles show "idle" with last completion timestamp
- All configured roles present even if idle

---

## Scenario 5 — Empty state / idle coordinare (FR-007)

### Setup
1. No cards in progress

### Expected
- Main dashboard shows "No active performers"
- Current phase (idle) visible
- Last poll timestamp visible
- Board column counts visible (e.g. "TODO: 3, DONE: 12")

### Verify
- No JavaScript errors in console
- No broken DOM elements
