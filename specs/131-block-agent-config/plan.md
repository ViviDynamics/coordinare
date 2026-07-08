# Implementation Plan: Block Agent Config Artifacts From Commits

**Branch**: `131-block-agent-config` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/131-block-agent-config/spec.md`

## Summary

Stop performer agents from committing their tool-config/state dirs (`.codex`, `.claude`,
`.hermes`, `.junie`, `.opencode`, `.openclaw`, `.pi`, …) into a symphony repo — **mechanically**,
in two layers:

1. **Prevention (US1)**: when the performer sets up a workspace clone, write the canonical
   agent-noise directory globs into the clone's **`.git/info/exclude`** (repo-local, untracked)
   so any `git add .` / `git add -A` silently skips them — without editing the target repo's
   tracked `.gitignore`.
2. **Guard (US2)**: at the commit/push boundary, detect any staged/committed agent-config path
   (which only happens via `git add -f` or an unlisted dir) and unstage/drop it — or fail
   loudly with a structured, deduplicated signal.

A single canonical list of agent-noise markers backs both the new exclusion/guard **and** the
existing review-diff filter (`_DIFF_NOISE_PATH_MARKERS`), enforced so they cannot drift.

## Technical Context

**Language/Version**: Python 3.12+ (performer package `agent/performer`, coordinare `src/coordinare`).
**Primary Dependencies**: the performer workspace/git layer
(`agent/performer/src/performer/workspace.py` — `_run_git`, the clone/config seam (~L220), the
`git add -- <path>` single + batch commit paths, push); the review-diff noise filter
(`src/coordinare/graph/nodes/dispatch_performer.py` `_DIFF_NOISE_PATH_MARKERS`). No new deps.
**Storage**: N/A — no persisted coordinare state. The exclusion is a per-clone file
(`.git/info/exclude`) inside the ephemeral workspace.
**Testing**: pytest. Unit tests for the shared marker set + the exclude-file writer + the guard
(pure detection); an integration-style test that runs real `git init`/`add -A`/`add -f`/commit
in a temp repo and asserts what does/doesn't get staged/committed; a drift-guard test.
**Target Platform**: git inside the performer container (Debian) + host test runs.
**Project Type**: single repo, two packages (coordinare host + performer container).
**Performance Goals**: negligible — one small file write at clone; a `git diff --cached
--name-only` scan at commit.
**Constraints**: must not modify the target repo's tracked `.gitignore`; match by dot-dir path
segment (no substring false positives, no blanket dotfile ignore); no per-backend/per-repo
config; must survive the package boundary (see below).
**Scale/Scope**: ~1 shared constant module, 1 exclude-file writer at the clone seam, 1 guard at
the commit/push boundary, extend `_DIFF_NOISE_PATH_MARKERS`, + tests, + 1 small persona note.

**Package-boundary decision (the one real design question)**: the diff filter lives in the
**coordinare** package and the enforcement lives in the **performer** package — they are not
co-imported at runtime. So "one source of truth" (FR-001/SC-005) is realized as: define the
canonical set in the **performer** package (where enforcement runs — `noise_paths.py`), keep
`_DIFF_NOISE_PATH_MARKERS` in coordinare, and add a **drift-guard test** asserting the coordinare
list stays consistent with the shared set. (If a direct import is clean in the runtime/test
environment, prefer importing the one constant; otherwise the drift test is authoritative.)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — ✅ small, single-responsibility helpers (marker set, exclude
  writer, guard); a mechanical guardrail over instruction (matches the project's recorded
  "durable external contract, models forget instructions" principle).
- **II. Testing Discipline (NON-NEGOTIABLE)** — ✅ unit tests for the marker set, the exclude
  writer, and the guard's detection; a real-git integration test for staged/`add -f`/commit
  behavior; a drift-guard test for the shared list. Coverage not decreased.
- **III. No hidden logic / minimal deps** — ✅ uses git's own ignore mechanism; no new deps.

No violations → Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/131-block-agent-config/
├── plan.md              # This file
├── research.md          # Phase 0 — .git/info/exclude vs core.excludesfile; matching; drift guard
├── quickstart.md        # Phase 1 — how to verify prevention + guard
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit.tasks)
```

`data-model.md` and `contracts/` are **N/A** — no persisted data model and no API/inter-service
contract; the "interface" is git's ignore behavior + the shared marker set.

### Source Code (repository root)

```text
agent/performer/src/performer/
├── noise_paths.py        # NEW — canonical AGENT_CONFIG_DIRS + NOISE_PATH_MARKERS (source of truth)
└── workspace.py          # EDIT — clone seam writes .git/info/exclude; commit/push guard
src/coordinare/graph/nodes/
└── dispatch_performer.py # EDIT — _DIFF_NOISE_PATH_MARKERS references/mirrors the shared set
agent/performer/tests/
├── test_noise_paths.py   # NEW — canonical set contents + extensibility
└── test_workspace_agent_ignore.py  # NEW — exclude writer + guard + real-git add/add -f/commit
tests/unit/
└── test_diff_noise_drift.py        # NEW — drift guard: coordinare list consistent w/ shared set
```

**Structure Decision**: enforcement (exclude writer + guard + canonical set) lives in the
**performer** package because that's where the workspace and the commits happen; coordinare's
diff filter mirrors the shared set and a drift-guard test keeps them consistent across the
package boundary.

## Approach detail

- **Canonical set** (`performer/noise_paths.py`): `AGENT_CONFIG_DIRS = (".codex", ".claude",
  ".hermes", ".junie", ".opencode", ".openclaw", ".pi")` plus build/vcs noise (`node_modules`,
  `vendor/bundle`, `.venv`, `__pycache__`, `.tmp`) → derive both the `.git/info/exclude` glob
  lines and the diff-filter markers from it.
- **Prevention**: at the clone seam (workspace.py ~L220, beside `git config user.name`), append
  glob lines (e.g. `.codex/` and `**/.codex/`) to `<clone>/.git/info/exclude`. Repo-local,
  untracked, out of band (FR-004). Anchored dir-name globs avoid substring false positives
  (FR-005).
- **Guard**: at push (and reusable after the agent's own commits), run `git diff --cached
  --name-only` and/or inspect the new commit's paths → if any path segment is a canonical
  agent-config dir, `git rm --cached -r` it (unstage/drop) and log
  `commit_guard.agent_artifact_stripped` (paths only), or halt with a clear reason
  (FR-006/007). No-op when clean (FR-008).
- **Diff filter**: extend `_DIFF_NOISE_PATH_MARKERS` to the full agent-dir set (today it only
  carries `.codex/`), sourced from / mirrored against the shared set; drift-guard test.
- **Persona note (optional, non-authoritative)**: a one-line reminder in the commit-oriented
  personas; explicitly not the mechanism (FR-010).

## Complexity Tracking

No constitution violations — section intentionally empty.
