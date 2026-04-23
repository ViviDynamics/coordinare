# Feature Specification: Operational Visibility & Hygiene

**Feature Branch**: `052-stale-branch-cleanup`
**Created**: 2026-04-21
**Status**: Draft
**Input**: Three independent operational gaps identified during live testing: (1) operators have zero visibility into what a performer subprocess is doing while it runs — the opencode web UI is available but undiscoverable; (2) stale remote branches from failed runs cause repeated 422 PR creation failures that operators must manually fix; (3) a class of cross-boundary payload bug (AgentService field-filtering) silently broke 6+ features without being caught by any spec, plan, or test.

## User Scenarios & Testing *(mandatory)*

---

### User Story 1 — Operator can see what the performer is doing in real time (Priority: P1)

When a performer is actively working on a card, the dashboard's performer details panel shows a clickable link to the backend's live UI (e.g. opencode's web interface at its local port), plus a live summary of progress (files changed, lines added/deleted, current session title). The operator no longer has to guess whether the performer is working or stuck.

**Why this priority**: During live testing, a performer ran for 30+ minutes with no visible activity. The opencode web UI was running at `http://127.0.0.1:<port>` but was never surfaced. The operator had no way to distinguish "working hard" from "stuck silently."

**Independent Test**: Start coordinare with an opencode performer. Dispatch a card. Open the dashboard performer panel. Verify: a clickable URL to the opencode UI appears; the session title, files changed, and line diff counts update at least every 30 seconds while the performer is active.

**Acceptance Scenarios**:

1. **Given** a performer subprocess is active and the backend supports a local UI, **When** the operator opens the performer details panel, **Then** a clickable link to the backend UI is shown (including port).
2. **Given** a performer is actively editing files, **When** the dashboard updates, **Then** the session summary shows the current session title, total files changed, and net lines added/deleted.
3. **Given** the backend does not expose a local HTTP API (e.g. a custom backend), **When** the dashboard renders, **Then** the backend UI link is absent and no error is shown — the panel degrades gracefully.
4. **Given** the performer process exits, **When** the dashboard next updates, **Then** the live UI link and session stats are cleared.

---

### User Story 2 — Stale branch cleaned up before workspace creation (Priority: P1)

When coordinare is about to dispatch a card to a performer, it checks whether the expected branch name already exists on the remote. If it does, it deletes the stale branch before creating the workspace, so the performer always starts from a clean state.

**Why this priority**: During live testing, stale branches from failed runs caused repeated PR creation failures (422 error). The operator had to manually delete branches to unblock coordinare. This happened multiple times per session.

**Independent Test**: Manually create branch `coordinare/CARD-89/add-auth` on the remote. Dispatch card #89 to the implementer. Verify coordinare detects the stale branch, deletes it, creates a fresh workspace, and successfully creates a PR.

**Acceptance Scenarios**:

1. **Given** a stale branch exists on remote for the card being dispatched, **When** coordinare begins workspace setup, **Then** the stale branch is deleted before the performer clones or creates the workspace.
2. **Given** no stale branch exists, **When** coordinare begins workspace setup, **Then** workspace creation proceeds normally (no change in behavior).
3. **Given** the stale branch deletion fails (e.g. permission error), **When** coordinare handles the failure, **Then** it logs a warning and continues — falling back to current behavior.
4. **Given** `stale_branch_cleanup: false` in config, **When** coordinare begins workspace setup, **Then** stale branch detection is skipped entirely.

---

### User Story 3 — Configurable branch collision strategy (Priority: P2)

Instead of always deleting, operators can configure coordinare to append a run counter to branch names to avoid collisions (e.g. `coordinare/CARD-89/add-auth-2`).

**Independent Test**: Set `branch_collision_strategy: suffix` in config. Dispatch the same card twice (second time while first branch still exists). Verify the second workspace uses a `-2` suffixed branch name.

**Acceptance Scenarios**:

1. **Given** `branch_collision_strategy: suffix` and a stale branch exists, **When** coordinare creates a new workspace, **Then** the branch name gets a numeric suffix (`-2`, `-3`, etc.).
2. **Given** `branch_collision_strategy: delete` (default), **When** a stale branch exists, **Then** the old branch is deleted and a fresh branch is created at the same name.

---

### User Story 4 — Speckit flags cross-boundary payload mismatches (Priority: P2)

When `speckit.analyze` runs on a feature that adds or removes fields from a cross-boundary payload (e.g. the coordinare → performer dispatch payload), it reports a warning if the receiving side's schema does not have a matching field definition in the contract registry.

**Why this priority**: The AgentService field-filtering bug silently broke 6+ features — new fields added to the dispatch payload were stripped before reaching the performer. No spec, plan, or test caught it. A contract-aware analyze step would have flagged it immediately.

**Independent Test**: Write a spec that adds a new field `foo_bar` to the dispatch payload but does not update `specs/contracts/dispatch-payload.md`. Run `speckit.analyze`. Verify the output includes a warning that `foo_bar` is not registered in the contract and the receiving side has no matching entry.

**Acceptance Scenarios**:

1. **Given** a spec adds a field to a cross-boundary payload and the contract registry is not updated, **When** `speckit.analyze` runs, **Then** a contract mismatch warning is emitted listing the unregistered field(s).
2. **Given** the spec updates both the payload and the contract registry consistently, **When** `speckit.analyze` runs, **Then** no contract warning is emitted.
3. **Given** a spec does not touch any cross-boundary payload, **When** `speckit.analyze` runs, **Then** the contract check is skipped silently.

---

## Functional Requirements

### Backend Transparency (US1)

- **FR-001**: When a `SubprocessTransport` session is active, scan the buffered stderr logs for the backend's port announcement pattern (e.g. opencode emits `"server":"http://127.0.0.1:<port>"` to stderr). Extract and return the full URL; return `null` if no matching line is found or the backend does not emit one.
- **FR-002**: Include a `backend_ui_url: str | None` field in the SSE state payload for each active performer session; set to the discovered URL or `null`.
- **FR-003**: Include a `session_stats: SessionStats | None` field in the SSE payload (`SessionStats` has `title: str | None`, `files_changed: int`, `lines_added: int`, `lines_removed: int`), populated by polling the backend's local HTTP API (if available) at most every 30 seconds.
- **FR-004**: The dashboard performer panel renders `backend_ui_url` as a clickable external link when non-null; renders `session_stats` as a compact summary line.
- **FR-005**: Backend transparency is read-only and best-effort — any error fetching stats is swallowed and logged at DEBUG; the performer session is never interrupted.

### Stale Branch Cleanup (US2 & US3)

- **FR-006**: Add `stale_branch_cleanup: bool` to `ProjectConfiguration` (default: `true`).
- **FR-007**: Add `branch_collision_strategy: Literal["delete", "suffix"]` to `ProjectConfiguration` (default: `"delete"`).
- **FR-008**: Before workspace creation in `WorkspaceManager.prepare()`, compute the expected branch name and call `GitHubService.branch_exists(branch_name) -> bool`.
- **FR-009**: Add `branch_exists(branch_name: str) -> bool` and `delete_branch(branch_name: str) -> None` to `GitHubService` using the REST API.
- **FR-010**: Log a structured `workspace.stale_branch_delete_attempted` event at INFO when deletion is attempted, including branch name and card_id.
- **FR-011**: If `branch_collision_strategy: suffix`, compute the next available suffix by checking remote branch existence up to `-9`; if all suffixes are taken, fall back to delete and log `workspace.suffix_exhausted_delete_attempted` at WARNING.

### Contract Enforcement in Speckit (US4)

- **FR-012**: `speckit.analyze` reads all `.md` files in `specs/contracts/` to build a registry of cross-boundary payload fields (keyed by contract name and field name).
- **FR-013**: During analysis, if the spec.md or plan.md references a payload field that is not present in the matching contract file, emit a `[CONTRACT MISMATCH]` warning listing the field name, the contract file it should appear in, and which side (sender/receiver) is missing the definition.
- **FR-014**: A new `speckit.contracts` command (or `speckit.analyze --contracts`) regenerates / validates `specs/contracts/*.md` against the current source code schemas — flagging fields present in code but absent from the contract, and vice versa.

## Success Criteria

- **SC-001** (US1): Backend UI link appears in the dashboard performer panel within one poll cycle (≤30 s) of the performer subprocess announcing its port; updating at most every 30 seconds thereafter.
- **SC-002** (US1): `speckit.analyze` and the stats-polling path add zero latency to the performer subprocess — any fetch error is silently swallowed and the performer is never blocked or restarted.
- **SC-003** (US2): Stale branch detection adds at most one GitHub REST API call per dispatch (`branch_exists`); the `delete_branch` call is made only when a stale branch is actually detected (expected: rare).
- **SC-004** (US4): `speckit.analyze` contract check completes in under 1 second for a typical spec (≤5 contract files, ≤200 field entries).

## Non-Goals

- Bulk cleanup of all stale branches (single-card scope only).
- Modifying performer subprocess behavior or injecting UI URLs into the performer environment.
- Enforcing contracts at runtime (this is a speckit design-time check only).
- Auto-generating contract files from code (contract files are maintained manually by the spec author).
