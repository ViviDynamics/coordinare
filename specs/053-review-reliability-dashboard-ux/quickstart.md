# Quickstart: Review Reliability and Dashboard UX Completion

## Scenario 1 - Reviewer JSON recovery avoids hard block (P1)

### Setup
1. Start coordinare with lifecycle including `reviewing`.
2. Configure performer backend test double (or fixture) to return non-JSON reviewer output on first completion and valid JSON on second completion.

### Expected
- First invalid output triggers `working` retry/recovery, not immediate `blocked`.
- Second valid output transitions to `approved` or `changes_requested` path.

### Verify
- Card phase does not enter `blocked` after first invalid reviewer output.
- Open questions do not contain terminal parse-failure message when recovery succeeds.

---

## Scenario 2 - Idle Active Performers tile shows board summary (P1)

### Setup
1. Ensure no active performer sessions.
2. Ensure `board_snapshot` has entries across TODO/IN_PROGRESS/IN_REVIEW/DONE.
3. Open dashboard `/`.

### Expected
- Idle tile shows phase, cycle count, board counts, and last poll time.

### Verify
- Values match snapshot counts.
- If `assignee_filter` is set, tile also shows filter hint.

---

## Scenario 3 - History page renders real data (P2)

### Setup
1. Let at least 3 cycles complete.
2. Navigate to `/history`.

### Expected
- Page shows cycle history table rows.
- "Coming soon" text is absent.

### Verify
- Table includes time, phase, duration, outcome.
- New cycles appear while remaining on `/history`.

---

## Scenario 4 - Performers page row drilldown (P2)

### Setup
1. Dispatch work so at least one role is active.
2. Navigate to `/performers`.

### Expected
- Clicking role row opens detail panel with live events/logs/metrics/backend link/session stats.

### Verify
- Back button returns to role list without full page reload.
- Detail panel remains stable while SSE updates stream.

---

## Scenario 5 - Desktop layout uses one-column workflow + performers cards (P2)

### Setup
1. Set browser width to >=900px.
2. Open dashboard `/`.

### Expected
- Workflow card and compact performers card each occupy one column side-by-side.

### Verify
- Cards are not full-width at desktop breakpoint.
- At mobile width, layout stacks vertically with no horizontal scrolling.
