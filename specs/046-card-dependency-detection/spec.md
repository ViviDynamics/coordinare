# Feature Specification: Card Dependency Detection

**Feature Branch**: `046-card-dependency-detection`  
**Created**: 2026-04-16  
**Status**: Draft  
**Input**: Card dependency detection and blocked_by awareness for multi-card orchestration. Assessor analyzes titles/descriptions to detect inter-card dependencies. Coordinare must not pick up a card whose dependencies haven't completed. Cards updated with blocked_by metadata. Dashboard/Slack surface dependency state.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Coordinare skips dependent cards in the TODO queue (Priority: P1)

When multiple cards are in the TODO column, the coordinare must not dispatch a card whose work depends on another card that is still in progress, in review, or in TODO. For example, if card #91 "Add dark mode toggle" depends on card #90 "Set up theming infrastructure", the coordinare should pick up #90 first and leave #91 in TODO until #90 reaches DONE.

**Why this priority**: Without this, the coordinare dispatches cards in arbitrary order and the implementer works against a codebase that doesn't yet have the prerequisite changes — leading to merge conflicts, wasted tokens, and cards that loop through feedback cycles because the underlying dependency hasn't shipped.

**Independent Test**: Create two cards where card B's description contains "Depends on #A". Place both in TODO. Verify the coordinare picks up card A first. Move card A to DONE. On the next poll cycle, verify card B becomes eligible and is dispatched.

**Acceptance Scenarios**:

1. **Given** two TODO cards where card B's body contains "Depends on #90" and card #90 is in TODO, **When** the coordinare polls the board, **Then** only card #90 is dispatched; card B remains in TODO.
2. **Given** card B depends on card #90 and card #90 is in IN_PROGRESS, **When** the coordinare polls, **Then** card B remains in TODO (not dispatched).
3. **Given** card B depends on card #90 and card #90 moves to DONE, **When** the coordinare polls on the next cycle, **Then** card B becomes eligible and is dispatched normally.
4. **Given** card B depends on cards #90 and #91, **When** only #90 is DONE but #91 is still IN_PROGRESS, **Then** card B remains in TODO until both are DONE.

---

### User Story 2 — Assessor detects implicit dependencies (Priority: P2)

When the assessor evaluates a card, it compares the card's title and description against the titles of other cards currently on the board (TODO, IN_PROGRESS, IN_REVIEW). If the assessor determines the card's work logically depends on another in-flight card — for example, "Add copy button to code blocks" depends on "Implement syntax highlighting for code blocks" — it flags the dependency and the card is blocked with a comment explaining which card it depends on and why.

**Why this priority**: Explicit "Depends on #N" syntax requires human discipline. Many real-world dependencies are implicit in the feature descriptions. The assessor's analysis catches these before the implementer wastes a cycle discovering the dependency through code conflicts or missing prerequisites.

**Independent Test**: Create card A "Implement base theming system" (IN_PROGRESS) and card B "Add dark mode toggle" (TODO) with no explicit dependency syntax. Dispatch card B to the assessor. Verify the assessor identifies the dependency and the card is blocked with a comment naming card A.

**Acceptance Scenarios**:

1. **Given** card B in TODO with title "Add dark mode toggle" and card A "Implement base theming system" is IN_PROGRESS, **When** the assessor evaluates card B, **Then** the assessor identifies card A as a dependency and returns a blocked status with the dependency explanation.
2. **Given** card B in TODO with no relationship to any other active card, **When** the assessor evaluates it, **Then** the assessor does not flag any dependencies and proceeds normally.
3. **Given** card B was blocked by dependency on card A which is now DONE, **When** card B is re-assessed, **Then** the assessor no longer flags the dependency and the card proceeds to dispatch.

---

### User Story 3 — Dashboard and Slack show dependency state (Priority: P3)

When a card is blocked because of a dependency, the dashboard shows which card(s) it's waiting on with clickable issue links. The Slack notification for the blocked card includes the blocker issue number(s) and their current status so the operator knows at a glance what's holding things up.

**Why this priority**: Visibility. Without this, the operator sees "card blocked" in Slack but has to manually cross-reference the board to understand why. With dependency visibility, the operator can immediately tell whether the blocker is close to finishing or stalled.

**Independent Test**: Block a card due to a dependency. Verify the dashboard shows the blocked card with its blocker(s) listed and linked. Verify the Slack notification includes the blocker issue numbers and their current column.

**Acceptance Scenarios**:

1. **Given** card B is blocked by dependency on card #90 (currently IN_PROGRESS), **When** the operator views the dashboard, **Then** card B shows "Blocked by #90 (IN_PROGRESS)" with a clickable link to issue #90.
2. **Given** card B is blocked by dependency, **When** the Slack blocked notification fires, **Then** the message includes the blocker issue number(s) and their current board column.
3. **Given** card B was blocked by #90 and #90 moves to DONE, **When** the dashboard refreshes, **Then** card B no longer shows the dependency block.

---

### Edge Cases

- **Circular dependencies**: Card A depends on card B, which depends on card A. The coordinare must detect the cycle and block both with a diagnostic comment naming the cycle, rather than silently deadlocking.
- **Dependency on a card not on the board**: "Depends on #50" but #50 is closed, merged, or not a project item. Treat closed/merged as satisfied (dependency met). If the issue doesn't exist or isn't on the project board, log a warning and treat the dependency as unresolvable — block the card with a comment explaining the referenced issue isn't tracked.
- **Multiple dependency syntax formats**: Support "Depends on #N", "Blocked by #N", "After #N", "Requires #N" (case-insensitive). These are the most common conventions in issue trackers.
- **Dependency on an issue in a different repository**: Out of scope for this spec. Only same-repo issue numbers are resolved. Cross-repo references are ignored.
- **Card removed from the board while a dependent card is in-flight**: If blocker card disappears (manually removed from project), treat the dependency as unresolvable and block the dependent card with a comment.
- **Assessor false positives**: The assessor may flag a dependency that doesn't exist (e.g., two cards with similar titles but genuinely independent). The human can move the card back to TODO with a comment clarifying there is no dependency, and the coordinare should resume dispatch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST parse card descriptions for explicit dependency syntax: "Depends on #N", "Blocked by #N", "After #N", "Requires #N" (case-insensitive). Multiple dependencies per card are supported.
- **FR-002**: System MUST exclude cards with unsatisfied dependencies from the TODO pickup queue. A dependency is "satisfied" when the referenced issue's card is in the DONE column or the issue is closed/merged.
- **FR-003**: System MUST re-evaluate dependency satisfaction on every board poll cycle, so a card becomes eligible as soon as its blockers complete.
- **FR-004**: The assessor role MUST compare the current card's title and description against titles of other active board cards (TODO, IN_PROGRESS, IN_REVIEW) and flag implicit dependencies when the card's work logically requires another card to complete first.
- **FR-005**: When the assessor detects a dependency (implicit or explicit), the card MUST be blocked with an `open_question` that names the blocking card(s) by issue number and explains the dependency.
- **FR-006**: System MUST detect circular dependencies among cards and block all cards in the cycle with a diagnostic comment naming the full cycle.
- **FR-007**: Dashboard MUST display dependency state for blocked cards: blocker issue number(s), their current board column, and clickable links to the issue.
- **FR-008**: Slack blocked-card notification MUST include blocker issue number(s) and their current board column.
- **FR-009**: When an explicit dependency references an issue that is already closed/merged, the dependency MUST be treated as satisfied (not blocking).
- **FR-010**: When an explicit dependency references an issue not found on the project board and not closed, the card MUST be blocked with a comment explaining the referenced issue is not tracked.

### Key Entities

- **CardDependency**: Represents a dependency relationship between two cards. Contains: dependent card ID, blocker issue number, dependency source (explicit syntax vs. assessor-detected), satisfaction status (pending / satisfied / unresolvable).
- **DependencyGraph**: The set of all active dependencies across cards on the board. Used for cycle detection and eligibility filtering. Rebuilt from scratch on each board poll (not persisted — derived from current board state + issue bodies).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Cards with unsatisfied dependencies are never dispatched to a performer. Zero false dispatches observed across a 24-hour multi-card test run.
- **SC-002**: When a blocking card reaches DONE, the dependent card is dispatched within 2 poll cycles (under 2 minutes at default 30s interval).
- **SC-003**: Circular dependencies are detected and both/all cards in the cycle are blocked with a diagnostic message within 1 poll cycle.
- **SC-004**: The assessor identifies at least 80% of implicit dependencies in a test set of 10 card pairs with obvious logical ordering (e.g., "set up X" before "use X").
- **SC-005**: Operator can determine why a card is blocked and which card it's waiting on within 10 seconds of viewing the dashboard or reading the Slack notification — no cross-referencing the board required.

## Assumptions

- Issue numbers in dependency syntax (#N) refer to issues in the same repository tracked by the coordinare's configured GitHub Project board.
- The assessor's implicit dependency detection is best-effort — it reduces wasted cycles but is not expected to catch every possible ordering constraint. Explicit "Depends on #N" is the authoritative mechanism.
- The dependency graph is rebuilt from card descriptions on every poll cycle rather than persisted. This keeps the implementation stateless (no new persistence schema) and self-healing (if a description is edited, the graph updates on the next cycle).
- Cross-repository dependencies are out of scope. A future spec may extend this to support org-wide dependency tracking.
- The term "multi-card mode" refers to coordinare operating with `max_concurrent_cards >= 2`, which is the existing feature from spec 035.
