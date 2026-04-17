# Quickstart: Card Dependency Detection

## Scenario 1 — Explicit dependency filtering (P1)

### Setup
1. Create two issues on the target repo:
   - Issue #101: "Set up theming infrastructure"
   - Issue #102: "Add dark mode toggle" — body includes "Depends on #101"
2. Add both to the GitHub Project board in the TODO column
3. Start coordinare with `max_concurrent_cards: 2`

### Expected behavior
1. Coordinare polls → sees both in TODO
2. Dependency parser extracts `#102 depends on #101`
3. Dependency graph marks #102 as PENDING (blocker #101 is in TODO, not DONE)
4. `eligible_todo` filters out #102 → only #101 is dispatched
5. #101 progresses through lifecycle → reaches DONE
6. Next poll → #102's dependency on #101 is now SATISFIED → #102 dispatched

### Verify
- Check coordinare log: `check_board.dependency_filtered` event with `filtered_count=1`
- Slack: only #101 dispatched initially
- After #101 reaches DONE: #102 dispatched on the next cycle

---

## Scenario 2 — Circular dependency detection

### Setup
1. Issue #103: "Module A depends on Module B" — body: "Depends on #104"
2. Issue #104: "Module B depends on Module A" — body: "Depends on #103"
3. Add both to TODO

### Expected behavior
1. Dependency graph detects cycle: #103 → #104 → #103
2. Both cards are blocked with a diagnostic comment: "Circular dependency detected: #103 → #104 → #103"
3. Neither is dispatched

### Verify
- Both cards move to BLOCKED on the board
- GitHub issue comments explain the cycle
- Slack notification fires for each blocked card

---

## Scenario 3 — Assessor implicit dependency (P2)

### Setup
1. Issue #105: "Implement syntax highlighting for code blocks" — in IN_PROGRESS
2. Issue #106: "Add copy button to code blocks" — in TODO, NO explicit dependency syntax
3. Start coordinare

### Expected behavior
1. #106 passes the explicit dependency filter (no "Depends on" in body)
2. Coordinare dispatches #106 to the assessor
3. Assessor receives active card titles including "#105 — Implement syntax highlighting..."
4. Assessor detects implicit dependency → returns `{"sufficient": false, "dependencies": [105], "questions": ["This card appears to depend on #105..."]}`
5. Card #106 is blocked with the assessor's dependency explanation

### Verify
- Coordinare log: `assess_card.dependency_detected` event
- GitHub issue #106 gets a comment from the assessor explaining the dependency
- Dashboard shows #106 blocked by #105

---

## Scenario 4 — Dependency on closed issue (satisfied)

### Setup
1. Issue #107: "Add search to blog" — body: "Depends on #50"
2. Issue #50 was completed months ago and is closed (not on the board)
3. Add #107 to TODO

### Expected behavior
1. Dependency parser finds `#107 depends on #50`
2. #50 not on board → coordinare checks GitHub API → issue #50 is closed
3. Dependency marked SATISFIED → #107 dispatched normally

### Verify
- Coordinare log: `dependency.off_board_check` with `issue=50, state=closed, satisfied=true`
- #107 dispatched without blocking

---

## Scenario 5 — Dashboard and Slack visibility (P3)

### Setup
1. Card #102 blocked by dependency on #101 (IN_PROGRESS)
2. Dashboard open in browser

### Expected behavior
1. Dashboard active-card area shows #102 with a "Blocked by" badge: "#101 (IN_PROGRESS)" as a clickable link
2. Slack blocked notification includes: "blocked: Depends on #101 (IN_PROGRESS)"
3. When #101 moves to DONE → dashboard updates → #102 no longer shows dependency block

### Verify
- Dashboard visually shows dependency state without scrolling
- Slack message contains blocker issue number and column
- Transition from blocked → dispatched is visible in real time via SSE
