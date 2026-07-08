# Research: Block Agent Config Artifacts From Commits (spec 131)

## Decision: `.git/info/exclude` as the out-of-band exclusion (not the tracked `.gitignore`, not `core.excludesfile`)

- **Decision**: Write the agent-noise globs into the clone's repo-local
  `<clone>/.git/info/exclude` at workspace-setup time.
- **Rationale**: `.git/info/exclude` is git's built-in **per-clone, untracked** ignore file —
  exactly "out of band" (FR-004): it never appears in `git status`, is never committed, and
  doesn't touch the target repo's tracked `.gitignore`. It applies to `git add .`/`-A`
  identically to `.gitignore`. Perfectly repo-agnostic (works on every symphony clone) with a
  single file write.
- **Alternatives considered**:
  - *Editing the repo's tracked `.gitignore`* — pollutes the user's repo / shows in the PR diff
    (violates FR-004). Rejected.
  - *`git config core.excludesFile <path>`* — also works and is global, but points at a file
    outside the clone; `.git/info/exclude` is simpler, self-contained per clone, and needs no
    config entry. (Either is acceptable; `.git/info/exclude` chosen for locality.)
  - *A container-image global gitignore baked into `$HOME/.gitignore` + `core.excludesFile`* —
    valid, but couples the guard to image builds; the per-clone write is testable in a temp repo
    without a container.

## Decision: match by anchored dot-directory path segment (no substring, no blanket dotfile ignore)

- **Decision**: Exclude entries are directory globs anchored to a path segment:
  `.<dir>/` and `**/.<dir>/` for each name in `AGENT_CONFIG_DIRS`. The commit guard matches a
  path iff one of its `/`-split segments equals a canonical dir name.
- **Rationale**: Prevents false positives (FR-005/SC-004): `.github/`, `.gitignore`,
  `.env.example`, `docs/claude-guide/` must NOT be excluded. Segment-equality on the dot-dir
  name is precise; the current `_DIFF_NOISE_PATH_MARKERS` uses substring (`mk in path`) which is
  looser — acceptable for the *display* filter but the enforcement guard must be exact.
- **Alternatives considered**: substring match (risk: `docs/claude-guide/` hit); leading-dot
  wildcard `.*/` (blanket dotfile ignore — explicitly out of scope). Rejected.

## Decision: two layers — prevention at clone, guard at commit/push

- **Decision**: Prevention (`.git/info/exclude`) handles the normal `git add .` case; the guard
  (`git diff --cached --name-only` scan + `git rm --cached -r`) handles the residuals a broad
  add can't: `git add -f <ignored>` and a not-yet-listed dir that a future backend introduces.
- **Rationale**: `.git/info/exclude` is bypassable by `-f`; the spec (US2, FR-006) requires that
  slipped-through artifacts still never reach a PR. A commit/push-boundary scan is the backstop.
- **Alternatives considered**: prevention only (leaves the `-f`/unknown-dir gap); guard only
  (works but noisier — every broad add would trip the guard instead of being silently clean).
  Both together = quiet in the common case, safe in the edge case.

## Decision: single source of truth across the coordinare/performer package boundary

- **Decision**: Define the canonical set in `agent/performer/src/performer/noise_paths.py`
  (where the enforcement runs). Coordinare's `_DIFF_NOISE_PATH_MARKERS` is kept in the coordinare
  package (it can't cleanly import the performer package at runtime), and a **drift-guard unit
  test** (`tests/unit/test_diff_noise_drift.py`) asserts the coordinare markers are consistent
  with the shared set (every canonical agent dir is represented as a marker).
- **Rationale**: FR-001/SC-005 want "one source that can't drift." A literal shared import is the
  ideal but the two packages deploy separately (performer runs in the container, coordinare on the
  host). The drift-guard test gives the same guarantee mechanically — CI fails if someone adds a
  dir to one list and not the other. If, in this repo's test/runtime layout, importing the
  performer constant into the coordinare test is clean, the test imports it directly (truly one
  definition); otherwise it encodes the expected mirror and checks both.
- **Alternatives considered**: duplicate lists with a comment "keep in sync" (drifts silently —
  this is exactly what caused `.codex/`-only coverage today). Rejected.

## Decision: guard action = strip-and-continue (default), with a loud path if stripping is unsafe

- **Decision**: Default behavior is to **unstage/drop** the agent-config paths and let the real
  change proceed (the common, safe repair), logging `commit_guard.agent_artifact_stripped` with
  paths only. Reserve a loud failure for a case where the change would become empty or the strip
  cannot be performed cleanly.
- **Rationale**: Silently producing a clean PR is the best UX; a hard failure on every stray
  `.codex/` would needlessly block otherwise-good work. FR-006 allows either; strip-and-continue
  is the better default, with the loud path as the safety valve. Paths-only logging satisfies
  FR-007 (no secrets/contents).
