# Contract: Documenter job (dispatch → result) with doc_mode

## Role mapping (reuse)
`tech_writer` (external) → `documenting` (stage) — `main.py:81-86`. Required JSON key `("files",)` — `main.py:87-94`.

## Dispatch payload additions
`Score.doc_mode: Literal["init","update"] = "update"` (`agent/performer/.../models.py`).
- Normal in-card documenting stage → `doc_mode="update"` (default; no caller change).
- Wiki-init bootstrap dispatch (`WikiInitService`) → `doc_mode="init"`.

## Doc-skip reconciliation (`dispatch_performer.py`)
Previously (spec 123): skip `documenting` when the PR diff touches no `docs/` path.

New behavior (spec 124, FR-006): **the documenting stage always runs.** The
documenter maintains the living `docs/wiki/` from the card's *code* changes, so a
code-only PR still needs a wiki refresh. The 123 docs-path skip (and its
`_should_skip_documenting`/`_documenter_backend` helpers) is therefore removed —
the documenter no-ops when there is genuinely nothing to record. This is
backend-agnostic: no per-backend gating remains.

## Documenter runs as plan→write (spec 124, C — performer-internal decomposition)
gpt-oss reliably handles small, focused calls but fails when asked to read the repo, judge significance, and emit *all* wiki pages as one giant JSON. So the documenting handler (`handle_status`) decomposes each run into small backend calls, across poll cycles (each poll returns fast):
1. **PLAN** (first backend run) — emits `{"pages": [{path, intent}], "deletions": [...]}` (tiny output; `{"pages": [], "deletions": []}` is the conservative no-op / significance gate).
2. **WRITE** (one backend run per planned page) — the handler builds a fresh, minimal write Score (`Score.doc_write_target = {path, intent, current}`, `pr_diff=""` so the context stays small) and hermes routes to a single-page prompt; each emits `{"files": [{path, content}]}` for that one page.
3. **COMMIT** — when the queue drains, the accumulated pages + the plan's deletions are batch-committed via `commit_files` → `docs_committed`.
This is **performer-internal**: no coordinare-graph / dispatch contract change. `doc_write_target` never crosses the coordinare→performer boundary. A page that fails to parse is logged + skipped (the rest commit); a malformed plan routes through the existing parse-retry.

## Documenter output contract (`{files}` + optional `deletions`)
The per-page WRITE + legacy single-shot path parse JSON in `main.py`'s documenting path:
- `files`: `[{path, content}]` — full-content writes/creates (unchanged, required key `("files",)`).
- `deletions` (optional, 124): `list[str]` of repo-relative paths to `git rm` in the SAME commit, letting the documenter RETIRE dead/low-value pages, not only rewrite them. **Restricted to `docs/`** by `workspace._safe_doc_deletions` (absolute / `..` / non-`docs/` paths are dropped with a log) so a wrong path can never remove source or root files. Backward-compatible: absent `deletions` behaves exactly as before.

## Result contract (reuse — no downstream change, SC-009)
`PerformerResponse`: `status="docs_committed"` + `files_modified: list[str]` (writes **and** deletions) on success, or `status="error"` + `reason`. (`protocol.py:101-120`)

## Test contract
- documenting stage with no `docs/` change → **dispatched** (never skipped) — `test_dispatch_performer.py::test_documenting_dispatched_even_without_doc_changes`.
- default `Score.doc_mode == "update"`; old payloads without the field validate — `test_score_doc_mode.py`.
- `doc_mode="init"` accepted for the wiki-init bootstrap dispatch.
- `deletions` git-rm's only safe `docs/` paths, in the same commit as writes, and no-ops on absent/unsafe paths — `test_commit_deletions.py`.
