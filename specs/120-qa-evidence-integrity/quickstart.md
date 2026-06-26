# Quickstart: Validating Spec 120

How to verify each user story. All automated checks are deterministic (fixtures/mocks — no live
containers). Run unit/integration with `.venv/bin/pytest` and lint with `.venv/bin/ruff check`.

## US1 — The gate rejects an unsubstantiated pass

**Unit (coordinare gate, `monitor_performer`):** feed synthetic terminal statuses and assert routing.

1. `status=qa_passed`, `report={criteria_checked:5, criteria_passed:0, environment_error:"ruby not found"}`
   → card HELD as `qa_env_blocked`, `mark_runtime_health_failed` called, notify fired, **no advance**.
2. Same but `environment_error=None`, `env_cache_health_failed=False`
   → routed to **bounce** (`qa_failed`), **no advance**.
3. `status=qa_passed`, `report={criteria_checked:5, criteria_passed:4, executed_checks:[...]}`
   → **advances** (legitimate pass, no regression).
4. `status=qa_passed`, `report={criteria_checked:3, criteria_passed:2, visual_validation_required:true, visual_evidence:[]}`
   → unsubstantiated (visual required, none captured) → HOLD or bounce per env signal, **no advance**.
5. `status=qa_passed`, `report={criteria_checked:0, criteria_passed:0, visual_validation_required:false}`
   → **advances** (genuine no-criteria scope — unchanged).

**Unit (performer advisory-pass, `main.py`):** build a QA output where all failures are environmental
and `criteria_passed==0` with some `executed_checks` → assert `PerformerResponse.status == "qa_env_blocked"`
(was `qa_passed`). With `criteria_passed>=1` → assert advisory `qa_passed` retained.

**Manual (live, website):** re-run QA on a card whose env is broken; the PR comment must read
ENVIRONMENT-BLOCKED/UNVERIFIED (or FAILED), never PASSED; the card holds, operator is notified.

## US2 — Toolchain on the QA performer's PATH

**Unit (`workspace._activate_env_cache`):** point at a fixture cache whose `activate.sh` sets a
`$DEVENV`-relative bin dir; assert the returned `cache_env["PATH"]` contains the resolved toolchain
dir even when the calling env has `_DEVENV_SOURCED=1` set (re-entry guard must be cleared and
`DEVENV` exported). Assert the observability event records `env_cache_path` present + activation
success + toolchain resolved.

**Unit (toolchain assertion):** a fixture cache that advertises rbenv but whose `ruby` does not
resolve → `_activate_env_cache` flags failure → the QA path surfaces `environment_error` /
`env_cache_health_failed` (which US1 then routes to HOLD).

**Manual (live, website):** dispatch QA; inside the container `ruby --version` resolves and the Rails
app boots. The dispatch observability record states the cache was attached and activation succeeded.

## US3 — Visual evidence (both paths)

**Unit (persona):** assert the qa persona text permits any in-image browser tooling (Chromium/
Playwright) for in-turn capture and does not mandate a single tool.

**Unit (`qa_screenshots` node):**
- App not reachable / no `app_boot_check` pass → node records "not reachable", does **not** set an
  empty `qa_screenshots=[]` masquerading as success.
- Docker unavailable → recorded honestly (not a silent empty success).
- App reachable + boot-proof present → node attempts capture.

**Enforcement (via US1):** a `visual_validation_required` change with zero `visual_evidence` cannot
yield an advancing `qa_passed` (covered by US1 case 4).

**Manual (live, website):** a UI card's QA run attaches ≥1 screenshot; a UI card with no screenshot
does not advance.

## Regression / gates

- `.venv/bin/pytest` — full suite green; coverage not decreased.
- `.venv/bin/ruff check <changed files>` — clean.
- Adversarial review (diverse-lens finders + refute-verify) before merge, per project discipline.
- Confirm no secret values appear in any new log/record/notification (grep the new events for
  value-bearing keys; only names/ids/counts/reasons permitted).
