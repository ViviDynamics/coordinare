# Feature Specification: Block Agent Config Artifacts From Commits

**Feature Branch**: `131-block-agent-config`
**Created**: 2026-07-08
**Status**: Draft
**Input**: User description: "All performer agents should know not to commit their own .folder (.codex, .claude, .hermes, .junie, etc.)"

## User Scenarios & Testing *(mandatory)*

Performer agents (the AI CLIs coordinare runs in containers) each keep tool config/state in a
dot-directory (`.codex`, `.claude`, `.hermes`, `.junie`, `.opencode`, `.openclaw`, `.pi`, …).
When an agent commits its work with a broad `git add .` / `git add -A`, those directories get
swept into the commit and land in the pull request. This must be prevented **mechanically** —
without relying on the agent to remember — so no agent-tooling artifact ever reaches a PR.

### User Story 1 - Agent config dirs are never staged (Priority: P1)

Regardless of how a performer's agent CLI stages changes (`git add .`, `git add -A`, an
editor integration, etc.), its own tool-config/state directories are silently skipped and
never enter a commit. This holds for every backend and on every project (symphony) repo,
without editing that repo's own ignore rules.

**Why this priority**: This is the root-cause fix and the MVP — if the dirs are never staged,
the pull request stays clean and the downstream context-window blow-ups disappear. It is
independently valuable on its own.

**Independent Test**: In a performer-style workspace, create an agent config dir (e.g.
`.codex/session.json`) alongside a real change, run a broad `git add -A` and commit, and
confirm the commit contains the real change but **none** of the agent-config paths — with the
project repo's own `.gitignore` untouched.

**Acceptance scenarios**:

1. **Given** a performer workspace with a real edited file and an agent config dir present,
   **When** the agent stages everything (`git add -A`) and commits, **Then** the commit
   includes the real file and excludes every agent-config directory.
2. **Given** any of the known backends (codex, claude_code, hermes, junie, opencode, openclaw,
   pi), **When** it runs in its workspace, **Then** that backend's config/state directory is on
   the ignore list and is not stageable by a broad add.
3. **Given** the project repo already has its own `.gitignore`, **When** the ignore rules are
   applied, **Then** the project's `.gitignore` is not modified (the exclusion is provided out
   of band, per workspace).
4. **Given** a legitimate project file or directory that merely starts with a dot (e.g.
   `.github/`, `.gitignore`, `.env.example`), **When** the agent commits, **Then** it is NOT
   excluded — only the known agent-tooling directories are.
5. **Given** the canonical list of agent-noise paths, **When** it is referenced by both the
   commit-time exclusion and the existing review-diff noise filter, **Then** both read from a
   single shared source (they cannot drift apart).

### User Story 2 - Slipped-through artifacts never reach a PR (Priority: P2)

If an artifact somehow gets staged anyway — an agent force-adds an ignored path
(`git add -f`), or a not-yet-listed tool dir appears — a guard at the commit/push boundary
catches it and removes it from the change (or fails loudly with a clear operator signal), so
agent-tooling junk never reaches a pull request or a merge.

**Why this priority**: Defense in depth. The P1 exclusion handles the normal case; this closes
the residual gaps (force-add, unknown dirs) and is what ultimately guarantees a clean PR — but
it only matters once the primary prevention exists.

**Independent Test**: Force-stage an agent-config path (`git add -f .codex/`) in a
performer workspace, run the commit/push path, and confirm the agent-config path is removed
from the pushed change (or the operation fails with a clear, deduplicated signal) while the
real change is preserved.

**Acceptance scenarios**:

1. **Given** an agent has force-added an ignored agent-config path, **When** the commit/push
   guard runs, **Then** the agent-config path is unstaged/dropped from the change and the real
   change proceeds, OR the operation halts with a clear operator-visible reason.
2. **Given** the guard removes or blocks something, **When** it acts, **Then** it emits a
   structured, deduplicated signal naming the offending path(s) — no secret/file contents.
3. **Given** only legitimate files are staged, **When** the guard runs, **Then** it is a no-op
   and adds negligible overhead.
4. **Given** the guard runs on a pushed change, **When** the resulting PR diff is later injected
   into a QA/review prompt, **Then** it contains no agent-tooling noise (the context-window
   blow-up that motivated this feature cannot recur from this source).

### Edge Cases

- **Agent config dir lives inside vs outside the repo**: some backends write their dir into
  `$HOME`, some into the working directory (the repo). Only in-repo occurrences can be
  committed; the exclusion must cover a config dir appearing at the repo root **or** nested.
- **Force-add** (`git add -f`) deliberately bypasses ignore rules → only the US2 guard catches
  it.
- **Legitimate dotfiles/dirs** (`.github/`, `.gitignore`, `.env.example`, `.ruby-version`)
  must never be excluded — the list is of specific known agent-tooling directories, not a
  blanket "ignore dotfiles" rule.
- **A new/unknown agent dir** not yet on the list → the US2 guard is the backstop; adding the
  dir to the single canonical list fixes it everywhere at once.
- **Repo that already has committed agent junk** (historical) → out of scope; the diff-noise
  filter remains as defense-in-depth for reading those.
- **Nested false positive**: a real project path that legitimately contains a segment like
  `claude` (e.g. `docs/claude-guide/`) must not be excluded — matching is on the specific
  dot-directory names, anchored to path segments, not substring.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: There MUST be a single, canonical, shared list of agent-tooling "noise" paths
  (the agent config/state directories plus the already-filtered build/vcs noise), referenced
  by both the commit-time exclusion (US1) and the existing review-diff noise filter — so the
  two cannot diverge.
- **FR-002**: The canonical list MUST include the known agent config/state directories for
  every supported backend: at least `.codex`, `.claude`, `.hermes`, `.junie`, `.opencode`,
  `.openclaw`, `.pi` (and remain trivially extensible when a backend is added).
- **FR-003**: Every performer workspace MUST be configured so that a broad stage
  (`git add .` / `git add -A`) does not stage any path in the canonical agent-config set,
  regardless of the agent CLI used.
- **FR-004**: The exclusion MUST be applied out of band (per workspace / performer
  environment) and MUST NOT modify the target project repository's own tracked ignore rules.
- **FR-005**: The exclusion MUST match only the specific known agent-tooling directory names
  (anchored to path segments) and MUST NOT exclude legitimate project files or directories
  that merely begin with a dot or contain a matching substring.
- **FR-006**: At the commit/push boundary, a guard MUST detect any staged or newly committed
  path within the canonical agent-config set and either remove it from the change or halt the
  operation with a clear, operator-visible reason (US2).
- **FR-007**: When the guard removes or blocks a path, it MUST emit a structured,
  deduplicated signal that names the offending path(s) only — never file contents or secrets.
- **FR-008**: When no agent-config paths are present, both the exclusion and the guard MUST be
  no-ops with negligible overhead and MUST NOT alter the real change.
- **FR-009**: The behavior MUST hold across all performer backends and all symphony repos
  without per-backend or per-repo configuration.
- **FR-010**: A persona/instruction reminder MAY be added, but MUST NOT be the enforcement
  mechanism — the mechanical exclusion + guard are authoritative.

### Key Entities

- **Agent-noise path set**: the single canonical collection of directory names/markers treated
  as non-committable tooling artifacts (agent config/state dirs + build/vcs noise), shared by
  the exclusion and the diff-noise filter.
- **Performer workspace**: the per-job checkout of a symphony repo in which an agent runs and
  commits; the unit where the exclusion is applied.
- **Commit/push guard**: the boundary check that verifies a change carries no agent-config
  paths before it becomes a PR.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a performer commits with a broad `git add`, 0 agent-config directories
  appear in the resulting commit/PR (across all supported backends).
- **SC-002**: The project repository's own ignore rules are unchanged by the feature (diff to
  the target repo's `.gitignore` is empty).
- **SC-003**: A force-added agent-config path does not reach a merged PR — it is removed or the
  operation halts with a clear signal (100% of the time in tests).
- **SC-004**: Legitimate dot-named project paths (`.github/`, `.gitignore`, `.env.example`,
  etc.) are never excluded (0 false positives in tests).
- **SC-005**: There is exactly one source of truth for the agent-noise path set; adding a new
  backend's dir in that one place updates both the exclusion and the diff filter.
- **SC-006**: The class of context-window failures caused by committed agent-tooling artifacts
  (e.g. the observed `.codex/`-junk QA blow-up) cannot recur from newly created PRs.

## Assumptions

- Agents commit with git inside the performer workspace; the workspace is a normal git
  checkout where per-repo git configuration (including an out-of-band excludes file) can be set.
- The canonical noise-path set currently lives as `_DIFF_NOISE_PATH_MARKERS` in the dispatch
  layer and is the natural home to centralize (or the natural source to extract a shared
  module from).
- "Out of band" exclusion means a per-workspace git ignore mechanism that does not require
  editing the checked-out repo's tracked files (e.g. a git excludes file or the repo-local
  info/exclude).
- Matching is by directory-name path segment (e.g. a top-level or nested `.codex/`), not
  substring, to avoid false positives.

## Out of Scope

- Changing where each backend stores its config/state (backend-internal).
- Removing the existing review-diff noise filter — it stays as defense-in-depth for repos that
  already contain committed agent junk.
- Scrubbing agent-tooling artifacts already committed in historical/merged PRs.
- Blanket "ignore all dotfiles" behavior.
