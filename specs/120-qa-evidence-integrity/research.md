# Research: QA Evidence Integrity, Toolchain Availability & Visual Evidence (Spec 120)

This document records the root-cause analysis performed during planning, grounded in the live
failure (website card #177, 2026-06-25) and verbatim reads of the current code. It resolves the
"how" for each user story before tasks are written.

## Decision 1 (US1): The false-PASS is the env-limited "advisory pass" path, not a missing zero-evidence check

**Finding.** The performer already enforces spec-088 zero-evidence integrity in
`_qa_unsubstantiated_pass()` (`agent/performer/src/performer/main.py:944`): a `qa_passed` claim with
*no* `executed_checks`, *no* `new_tests`, and *no* `visual_evidence` is refused (→ `qa_env_blocked`
when env-limited, else a failure). That check did **not** catch #177.

**Why #177 still passed.** In the QA terminal block (`main.py` ~L2500–2743):
- Every reported failure was reclassified environmental (`_qa_failure_is_environmental`), so
  `defect_failures == []` → `qa_passed_flag = True`.
- The run carried *some* evidence (the failed command attempts in `executed_checks`), so
  `_qa_unsubstantiated_pass()` returned `False` (it only guards the *zero-evidence* case).
- `env_limited` was `True`, so control reached the **advisory-pass** branch (~L2731): it logs
  `qa.env_limited_advisory_pass` and returns `status="qa_passed"` — **with `criteria_passed == 0`**.

So a run that verified **zero** acceptance criteria, under a broken environment, is emitted as a
terminal success. The coordinare (`monitor_performer.py`) lists `qa_passed` in
`TERMINAL_SUCCESS_STATES` (L44) and advances on it (~L3229) **without reading `report.criteria_passed`
or `report.environment_error`** — it trusts the status string.

**Decision.** Close the hole in two layers (defense in depth):
1. **Performer (root cause):** the env-limited advisory pass must require *positive* verification.
   If `env_limited` and `criteria_passed == 0` (nothing actually passed), classify as
   `qa_env_blocked` rather than `qa_passed`. The advisory pass remains valid only when ≥1 criterion
   genuinely passed under partial env limits.
2. **Coordinare (durable gate):** before advancing on `qa_passed`, read `status["report"]`; if
   `criteria_checked > 0 and criteria_passed == 0` (or required visual evidence is absent for a
   visual change), **do not advance** — route to the existing `qa_env_blocked` HOLD path when an
   environment signal is present (`report.environment_error` or `env_cache_health_failed`),
   otherwise treat as `qa_failed` (bounce). This also protects against a stale/buggy performer.

**Rationale.** The performer fix removes the false positive at its source; the coordinare gate makes
QA-integrity a property the orchestrator owns and can prove, independent of any single performer
build. Both reuse existing states (`qa_env_blocked`, `qa_failed`) and the existing HOLD/notify
machinery — no new lifecycle.

**Alternatives considered.**
- *Coordinare-only gate.* Rejected: leaves the misleading `qa.env_limited_advisory_pass` and the
  PASSED-rendered PR comment in place; the performer would keep emitting a wrong verdict.
- *Performer-only fix.* Rejected: the coordinare would still blindly trust the status string, so any
  future performer regression silently re-opens the hole. The spec asks the orchestrator to own the
  gate (FR-001/FR-002).

## Decision 2 (US2): Make env-cache activation resolve `$DEVENV` deterministically + assert the toolchain

**Finding.** The dispatch path *does* attach the env-cache volume and set `env_cache_path` for the
`qa` stage — there is no qa-specific exclusion (`dispatch_performer.py` ~L1169: only `env_bootstrap`
is exempt; all consumers, including qa, are gated by the 077 "current+verified" check and the 093
`verify_env_cache_clean` re-run, then receive the mount). And `claude_code` appends `cache_env["PATH"]`
after the image PATH for its Bash tool (`backends/_env_policy.py:build_subprocess_env`). So the
*plumbing* to put Ruby on the QA Bash-tool PATH exists.

**The break.** `cache_env` is produced by `_activate_env_cache()` (`workspace.py` ~L235), which sources
`activate.sh` via `bash -c "source activate.sh && env -0"`. `activate.sh` resolves the toolchain
through **`$DEVENV`** (e.g. `$DEVENV/.rbenv/versions/3.4.2/bin`, `$RBENV_ROOT/shims`). If `$DEVENV` is
not set in that subshell — or the image profile's re-entry guard (`_DEVENV_SOURCED=1`, which the
performer process inherits after its own startup activation) suppresses the profile that would set it —
the rbenv paths collapse to bogus values and `cache_env` comes back **without Ruby on PATH**. The QA
agent then correctly reports "Ruby runtime is not installed." (Note: `_start_env_cache_services`
already learned this lesson and clears `_DEVENV_SOURCED` before running — `workspace.py` ~L109 comment;
`_activate_env_cache` does not.)

**Decision.**
1. `_activate_env_cache()` must source `activate.sh` with `DEVENV=<env_cache_path>` exported and the
   `_DEVENV_SOURCED` re-entry guard cleared, so `$DEVENV`-relative paths resolve regardless of the
   inherited profile state (mirrors the existing `_start_env_cache_services` fix).
2. After activation, assert the project toolchain is resolvable (the cache advertises what it
   contains — e.g. an `activate.sh` that references rbenv ⇒ `ruby` must resolve on the resulting
   PATH). If the expected toolchain does **not** resolve, surface this as an environment failure that
   the QA run reports as `environment_error` / `env_cache_health_failed` (feeding US1's HOLD routing)
   rather than proceeding to a misleading verdict.
3. Emit an observability record per QA dispatch: whether `env_cache_path` was present, whether
   activation succeeded, and whether the toolchain resolved — names/reasons only (FR-010).

**Rationale.** The fix is at the one place that converts the (correct) on-disk cache into the
performer's runtime env, and it generalizes the proven `_DEVENV_SOURCED` clearing already used for
services. The toolchain assertion turns a silent empty-cache into an honest environment error, which
US1 then routes to HOLD instead of a false pass. The observability record makes the next occurrence
diagnosable from logs, not a live container dig (SC-004).

**Alternatives considered.**
- *Set `BASH_ENV` for the claude subprocess so every Bash-tool command re-sources the profile.*
  Rejected: claude_code deliberately snapshots launch PATH (the comment at `claude_code.py` notes the
  image node must win at launch); re-sourcing per command risks re-introducing the project-node-crashes
  -the-CLI problem 088/087 fixed. Activating once into `cache_env` (current design) is correct; we just
  need that activation to actually capture Ruby.
- *Re-bootstrap the cache.* Rejected: the cache is confirmed correct on disk; the defect is activation,
  not contents (explicitly out of scope).

## Decision 3 (US3): In-performer capture is the primary path; harden the docker node as an honest backstop

**Finding.** The QA persona already *requires* `app_boot_check` + ≥1 `visual_evidence` for
`visual_validation_required` changes (`persona_service.py` qa block) — so in-performer capture is
already permitted. #177 failed it only because the app could not boot (US2). The separate post-QA
node `qa_screenshots.py` launches a **Playwright docker image** via `launch_docker_env` against
`workspace_path`; when docker is unavailable it **silently sets `qa_screenshots=[]`** and returns —
exactly the "no artifacts, no error" outcome #177 showed ("Docker is not available").

**Decision.**
1. **Primary (in-performer):** clarify the QA persona that the performer may use *whatever* browser
   tooling is already in the image (Chromium/Playwright) to boot the app and capture ≥1 screenshot —
   no single mandated tool ("however is easiest"). Once US2 lands, the frontier QA model can do this.
2. **Backstop (docker node):** harden `qa_screenshots` to (a) run only when there is boot-proof
   (the QA report's `app_boot_check` passed), and (b) never report success with zero artifacts —
   when capture is not possible (docker unavailable, app unreachable) it records that fact rather
   than an empty success.
3. **Enforcement:** for a `visual_validation_required` change, zero `visual_evidence` ⇒ not a pass.
   This is enforced by US1's gate (FR-016 via FR-002), so the two paths are belt-and-suspenders while
   the gate provides the teeth.

**Rationale.** The capture itself was never the gate — the absence of enforcement was. Making the
in-performer path explicit (and unblocking it via US2) gets real screenshots; the hardened backstop
stops the silent empty-success; US1 guarantees a screenshot-less visual change cannot pass.

**Alternatives considered.**
- *Replace the docker node with in-container Chromium as the backstop.* Deferred: the ephemeral QA
  container may have already exited post-run, so a reliable backstop against the *live* app belongs
  in-performer (path 1). The docker node stays as the out-of-band backstop, hardened to be honest.
- *Drop the docker node entirely.* Rejected: the user chose "both (defense in depth)."

## Cross-cutting

- **Secret invariant (FR-019):** all new records/logs carry names/ids/counts/reasons only; the existing
  QA observability already truncates `environment_error` to 200 chars and never logs values — new
  records follow suit.
- **No new dependency (FR-017):** Chromium/Playwright are in `:extra`; the docker node already exists.
- **State model (FR-018):** the QA report dict is already an open container on `PerformerResponse`
  (`report: dict | None`, no `extra="ignore"`), and the coordinare gate reads existing fields
  (`criteria_checked`, `criteria_passed`, `environment_error`, `visual_evidence`,
  `visual_validation_required`) — no schema migration. Any new flag is a backward-compatible default.
