# Research: Review Reliability and Dashboard UX Completion

## R1: Role-aware backend prompt tail

**Decision**: Replace the universal backend prompt tail ("Commit your changes...") with role-aware endings.

- Implementer-like stages keep commit-oriented instructions.
- Reviewer/assessor/security/qa/documenting-style stages end with strict output-contract instructions (JSON object only, no narration).

**Rationale**: Current universal tail conflicts with reviewer persona and can bias model output toward implementation narration instead of structured JSON.

**Alternatives considered**:
- Keep current tail and rely only on persona text: rejected as too fragile.
- Hardcode per-backend prompts outside shared builder: rejected due duplication across Codex/OpenCode/Claude adapters.

## R2: JSON format recovery strategy

**Decision**: On JSON parse failure for schema-required stages, attempt format recovery before terminal failure.

Recovery flow:
1. Parse raw output (existing extraction strategies).
2. If parse fails, send a focused repair prompt to the running backend (or controlled restart) asking for the exact JSON schema only.
3. Re-parse and continue if valid.

**Rationale**: Non-JSON failures are frequently formatting drift, not task failure. A repair turn is cheaper and more reliable than immediately blocking.

**Alternatives considered**:
- Immediate hard error after parse failure: rejected (current pain point).
- Unlimited retries: rejected (risk of loops and hidden failures).

## R3: Error classification in monitor_performer

**Decision**: Classify backend format failures as retryable system errors before final human block.

**Rationale**: Transport/system issues already route through `system_error`; format-contract failures are operationally similar and should get one self-heal chance.

**Alternatives considered**:
- Keep routing all performer `error` statuses directly to `blocked`: rejected due low resilience.

## R4: Idle observability model for Active Performers card

**Decision**: Use existing `state.board_snapshot` + `state.last_poll_at` to build and render `BoardSummary` in the idle tile.

**Rationale**: Required data already exists in state graph; no new API or storage needed.

**Alternatives considered**:
- Add dedicated `/api/board-summary` endpoint: rejected as unnecessary.

## R5: History page completion

**Decision**: Render `/history` from existing `cycle_history` SSE data (same schema already shown in dashboard "Recent Cycles" card), with an explicit empty state.

**Rationale**: Fastest path to remove stub while preserving current architecture.

**Alternatives considered**:
- Introduce persisted long-range history store: deferred, out of scope for this fix-focused feature.

## R6: Performers page drilldown reuse

**Decision**: Reuse existing performer detail rendering logic/components from dashboard performers card for `/performers` row selection.

**Rationale**: Avoids duplicated log/metrics rendering logic and keeps behavior consistent.

**Alternatives considered**:
- Build separate detail component for `/performers`: rejected due duplicate maintenance.

## R7: Workflow + performers layout simplification

**Decision**: Remove mandatory expand/collapse interaction as a layout crutch; place workflow and compact performers as one-column cards in the desktop grid.

**Rationale**: Matches operator request and restores above-the-fold signal density.

**Alternatives considered**:
- Keep full-width cards with collapse controls: rejected (current UX complaint).
