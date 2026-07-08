# Tasks: Block Agent Config Artifacts From Commits

**Feature**: `131-block-agent-config` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Tests included (constitution NON-NEGOTIABLE; TDD for the pure pieces). `[P]` = parallelizable
(different files, no incomplete deps).

## Phase 1: Setup

- [x] T001 Green baseline: `env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest tests/unit tests/contract -q` + performer tests `uv run pytest agent/performer/tests -q` + `.venv/bin/ruff check src tests`. Confirm the current `_DIFF_NOISE_PATH_MARKERS` value (only `.codex/` today) and the workspace clone seam (`agent/performer/src/performer/workspace.py` ~L220, the `git config user.name` block).

## Phase 2: Foundational (blocks both stories)

- [x] T002 [P] Create `agent/performer/src/performer/noise_paths.py` — the single source of truth: `AGENT_CONFIG_DIRS = (".codex", ".claude", ".hermes", ".junie", ".opencode", ".openclaw", ".pi")`; `BUILD_VCS_NOISE = ("node_modules", "vendor/bundle", ".venv", "__pycache__", ".tmp", ".git")`; helpers `exclude_globs()` (→ `.codex/`, `**/.codex/`, … per dir) and `path_has_agent_config(path)` (segment-equality match on `AGENT_CONFIG_DIRS`, NOT substring). Docstring: adding a backend's dir here updates prevention + guard + diff filter.
- [x] T003 [P] TDD test `agent/performer/tests/test_noise_paths.py`: assert every supported backend dir is in `AGENT_CONFIG_DIRS`; `exclude_globs()` yields anchored dir globs; `path_has_agent_config` matches `.codex/session.json`, `a/b/.claude/x`, bare `.pi/` but REJECTS `.github/`, `.gitignore`, `.env.example`, `docs/claude-guide/x` (no substring/false-positive). Make T002 pass.

## Phase 3: US1 — Agent config dirs are never staged (P1) 🎯 MVP

**Goal**: a broad `git add .`/`-A` in any performer clone never stages an agent-config dir,
without touching the repo's tracked `.gitignore`.

**Independent test**: real `git init` temp repo + `.git/info/exclude` written from
`exclude_globs()` → `git add -A` stages the real file, excludes `.codex/`; `.gitignore` unchanged.

- [x] T004 [US1] TDD test `agent/performer/tests/test_workspace_agent_ignore.py` (US1 part): with a real temp git repo, write the exclude file via the new writer, create `.codex/`, `.claude/` + a real file + a legit `.github/` + `.env.example`, `git add -A`, and assert staged set = {real file, `.github/…`, `.env.example`} and excludes the agent dirs; assert the tracked `.gitignore` is absent/unchanged. (Fails until T005.)
- [x] T005 [US1] Implement the exclude writer in `agent/performer/src/performer/workspace.py`: at the clone seam (beside `git config user.name`), append `noise_paths.exclude_globs()` lines to `<clone>/.git/info/exclude` (idempotent — don't duplicate on re-setup). Fail-safe: a write error logs a warning but does not abort the clone. Make T004 pass.

## Phase 4: US2 — Slipped-through artifacts never reach a PR (P2)

**Goal**: force-added (`git add -f`) or unlisted agent-config paths are stripped (or the op
halts loudly) at the commit/push boundary, with a paths-only structured signal.

**Independent test**: `git add -f .codex/x` then run the guard → `.codex/` unstaged, real change
kept, `commit_guard.agent_artifact_stripped` logged; a clean change → guard is a no-op.

- [x] T006 [US2] TDD test `agent/performer/tests/test_workspace_agent_ignore.py` (US2 part): force-add an agent-config path in a temp repo, run the guard, assert it is removed from the index (and any just-made commit) while the real change survives; assert a paths-only log signal; assert no-op + negligible work when nothing offending is staged. (Fails until T007.)
- [x] T007 [US2] Implement the commit/push guard in `agent/performer/src/performer/workspace.py`: before push (and after the batch/agent commit path), scan `git diff --cached --name-only` (and the new commit's paths) with `noise_paths.path_has_agent_config`; `git rm --cached -r` offending paths (strip-and-continue default), log `commit_guard.agent_artifact_stripped` (paths only, deduped); if stripping can't be done cleanly / would empty the change, halt with a clear operator reason. No-op when clean. Make T006 pass.

## Phase 5: Polish

- [x] T008 [P] Extend `_DIFF_NOISE_PATH_MARKERS` in `src/coordinare/graph/nodes/dispatch_performer.py` to cover the full agent-dir set (currently only `.codex/`), sourced from / mirrored against the shared set.
- [x] T009 [P] Drift-guard test `tests/unit/test_diff_noise_drift.py`: assert every `AGENT_CONFIG_DIRS` entry is represented in `_DIFF_NOISE_PATH_MARKERS` (import the shared constant if cleanly importable; else encode the mirror) so the two lists cannot drift. 
- [x] T010 [P] Add a one-line, non-authoritative persona reminder ("do not commit your tool-config dir") to the commit-oriented persona text; comment that the mechanical exclusion + guard are the real enforcement (FR-010).
- [x] T011 Full suite green (unit + **contract** + performer tests) + `ruff` clean; manual smoke per [quickstart.md](./quickstart.md); adversarial review before merge; confirm coverage not decreased.

## Dependencies

Phase 1 → Phase 2 (shared set) → US1 (prevention) → US2 (guard; reuses the shared set + the
temp-repo test file). Polish last. **MVP = US1** (never-staged prevention already eliminates the
normal case and the motivating incident). US2 is the backstop for force-add / unknown dirs.

## Notes

- **Run the WHOLE `tests/` tree** (unit + contract) AND the performer tests before pushing.
- Match by **path segment equality** on the dot-dir name — never substring, never blanket
  dotfile ignore (FR-005; avoids `.github/` / `docs/claude-guide/` false positives).
- Prevention writes `.git/info/exclude` (repo-local, untracked) — NEVER the tracked `.gitignore`
  (FR-004).
- Single source of truth = `performer/noise_paths.py`; the coordinare diff filter mirrors it and
  the drift-guard test (T009) fails CI if they diverge.

## Notes (post-adversarial-review)
Review verified the core mechanism sound (segment matching, guard git-correctness, drift test,
push integration). Three coverage findings, resolved:
- **Worktree `.git`-as-file (Medium)** — FIXED: `_write_agent_ignore` now skips when `.git`
  isn't a directory (with a debug log) instead of erroring. `clone_repository` never produces a
  worktree, and `strip_agent_artifacts` (push guard) backstops such repos regardless. Regression
  test added.
- **`commit_file`/`commit_files` don't run the guard (Low)** — ACCEPTED by design: these
  coordinare-driven paths stage only *explicitly-named* files (the documenter's `docs/` files,
  specific named files) via `git add -- <path>` and filter gitignored paths; they can never sweep
  an agent-config dir via a broad `git add .`, so there is nothing to guard.
- **`cdn_upload` qa-assets clone (Low)** — ACCEPTED by design: that separate clone commits only
  uploaded screenshot images to the `qa-assets` orphan branch; agent-config dirs cannot reach it.
