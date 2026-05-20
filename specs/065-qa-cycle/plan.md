# Plan: QA Cycle 065

Rolling QA iteration on coordinare, started after 063/064 landed. Fixes are
appended as live testing surfaces them; each fix is self-contained.

## Fix 1 — Dashboard env-bootstrap 503 / LangGraph state-merge schema (US1 side-effect)

**Symptom**: dashboard force-bootstrap returned 503 ("Env cache service not
available" / "Bootstrap performer ... is not registered") even though the
daemon's `_run` had populated those handles on the initial state dict.

**Root cause**: `env_cache_service` and `performer_services_by_id` were not
declared on the `CoordinareState` TypedDict. LangGraph's `StateGraph.ainvoke()`
filters incoming state to keys declared in the schema, so the first graph
cycle silently dropped both handles. The dashboard read state *after* the
first cycle and saw the keys missing.

**Fix**: add both keys to `CoordinareState` and seed them in `initial_state()`
(`env_cache_service: None`, `performer_services_by_id: {}`).

**Tests** (`tests/unit/test_state.py`):
1. Annotations check — both keys present on `CoordinareState.__annotations__`
   with explanatory error messages.
2. Seed check — `initial_state()` returns both keys with falsy defaults.
3. End-to-end LangGraph check — a real `StateGraph(CoordinareState)` cycle with
   a passthrough node preserves sentinel values for both keys across
   `compiled.ainvoke()`.

## Fix 2 — IN_PROGRESS fall-through for multi-card pickup (US2, FR-008a)

**Symptom**: with `max_concurrent_cards=3` on the `website` symphony and
multiple unblocked TODO cards available, only one card was ever pulled into
`active_sessions` at a time. The remaining performers stayed idle on
`/performers`.

**Root cause**: `src/coordinare/graph/nodes/check_board.py` lines 380-449
(pre-fix) — the IN_PROGRESS branch returned early in every path (preserved
phases, orphan re-adopt, `monitoring_agent` default). The 035 multi-card
TODO pickup at line ~794 was never reached when any IN_PROGRESS card existed.
The 061 IN_REVIEW branch already has the correct fall-through pattern: it
re-adopts IN_REVIEW cards as `monitoring_pr` sessions (passive, no slot
consumed) and falls through to TODO pickup when the bootstrap invocation
doesn't carry an IN_REVIEW `current_card`.

**Fix**: mirror the 061 pattern in the IN_PROGRESS branch.
- When `max_cards > 1`:
  - Re-adopt every uncovered IN_PROGRESS card into `active_sessions` with
    `phase="monitoring_agent"` (slot-consuming).
  - If `current_card` is itself one of the IN_PROGRESS cards (per-session
    invocation), preserve `phase="monitoring_agent"` and return.
  - Otherwise (bootstrap invocation, or current_card belongs to a different
    column), fall through to the TODO pickup at line ~794 so remaining
    concurrency slots fill in the same cycle.
- When `max_cards == 1`: preserve the existing single-card branch verbatim
  (orphan re-adopt + dispatch on first IN_PROGRESS, otherwise route to
  `monitoring_agent`).

**Tests** (`tests/unit/graph/nodes/test_check_board.py`):
1. `test_check_board_multicard_readopts_in_progress_and_picks_up_todos` —
   one IN_PROGRESS + two TODO + cap=3 → three `active_sessions` (one
   monitoring_agent + two dispatching).
2. `test_check_board_multicard_per_session_in_progress_preserves_monitoring_agent` —
   per-session invocation with `current_card` in IN_PROGRESS keeps
   `phase=monitoring_agent` and does NOT clobber `current_card` with a TODO
   pickup.
3. `test_check_board_singlecard_in_progress_still_short_circuits` —
   `max_cards == 1` behaviour unchanged (TODO cards not picked up while an
   IN_PROGRESS card exists).

## Fix 3 — Advisory comment dedup (security performer)

**Symptom**: on `ViviDynamics/website` PR #133 the security pass posted
the same `[Advisory - Security] **OWASP A04** (medium)` advisory every
cycle (~15-20 min) for ~8 hours, each one followed by a `QA Evidence:
PASSED`. After the operator's triage comment listing the *real*
outstanding items, the next 4 cycles ignored the triage and kept
re-posting the same advisory. Around 21:15 the same loop restarted with
a new topic (`OWASP A01 reviewer_id`) and ran 8 more times.

**Root cause**: `agent/performer/src/performer/main.py` (security role,
the advisory-posting block formerly at lines 1309-1320) called
`post_pr_comment` unconditionally for every medium/low finding emitted
by the security backend. The handler never queried the PR to see which
advisories had already been posted, and the backend prompt did not
include prior PR comments either, so each cycle generated a fresh
finding from the same code and the handler posted it again.

**Fix**:
1. Add `list_pr_comments(owner, repo, pr_number, token)` to
   `agent/performer/src/performer/github.py` — GET issues comments
   endpoint, first page (per_page=100) is sufficient.
2. In the security advisory block, fetch existing PR comments before
   the post loop and build a set of fingerprints from any prior
   `[Advisory - Security] **<category>** (<severity>)` headers.
3. Fingerprint = OWASP code (`a04`, `a01`, …) when present in the
   category, else slugified category, joined to severity. Phrasing
   drift in the description ("Insecure Design" vs "Insecure Design /
   Billing integrity") collapses to the same fingerprint.
4. Skip posting when fingerprint already in the set; record posted
   fingerprints to suppress intra-batch duplicates as well.
5. List failure (token missing, transient API error) is logged but does
   not block posting — preserves prior behaviour as a fallback.

**Tests** (`agent/performer/tests/unit/test_main.py`):
1. `test_advisory_comments_dedup_against_existing_pr_comments` — prior
   `OWASP A04 Insecure Design` advisory in `list_pr_comments` mock;
   finding with category `OWASP A04: Insecure Design` (different
   phrasing) → `post_pr_comment` not called.
2. `test_advisory_comments_posted_when_no_matching_existing` — prior
   A04 advisory; finding is A07 → `post_pr_comment` called once.
3. `test_advisory_comments_intra_batch_dedup` — two same-class findings
   in one batch with no prior comments → one post.

**Out of scope (separate follow-ups)**:
- Feeding the human reviewer's Request-Changes comments into the
  security/closer backend prompt so the agent sees what humans
  actually flagged. (Triage from PR #133 surfaced this — none of
  Jason's 7 bullets appeared in any post-triage advisory.)
- Wiring QA `PASSED` evidence back into a per-finding resolution
  ledger so the security pass can stop re-raising resolved items.

## Fix 4 — Closer feedback loop (empty-comments changes_requested)

**Symptom**: on `ViviDynamics/website` PR #133, three cards (74/101/the 9Tg
card) cycled implementer → reviewer → security → qa → tech_writer →
closer → *back to implementer* repeatedly. The closer kept returning
`status=changes_requested` with `comment_count=0`, so coordinare relayed
empty feedback to the implementer, the lifecycle re-ran end-to-end, the
closer rejected again. Loop, with no actionable signal to the operator.

**Root cause** (four overlapping bugs):

1. **Closer is doing 064's job**. The closer's backend produced
   `approved=false` with a body like *"No open review threads remain
   unresolved. RuboCop reported no offenses. Approval is withheld only
   because required/status check rollup is still pending, with five
   jobs in progress."* The closer's prompt instructs it to gate on
   remote CI status — but that's exactly what spec 064's PR-checks gate
   exists to handle. Intended layering: closer approves the *code*, then
   064 *holds* the handoff until remote checks pass. By rejecting on
   pending CI, the closer bounces the card all the way to `implementing`
   for a problem the implementer cannot fix.

2. **`review_body` is dropped on `changes_requested`**.
   `agent/performer/src/performer/main.py:1222` posts
   `f"**Bot Review: {verdict}**\n\n{review_body}"` to GitHub, but
   `PerformerResponse(status="changes_requested",
   session_id=perf.session_id, comments=comments)` at line 1262-1266
   only forwards the structured `comments` list. The whole "here's why
   I'm rejecting" paragraph reaches GitHub but never reaches coordinare,
   so coordinare sees an empty rejection and treats it as actionable.

3. **No safety net for empty-feedback bounces**. The coordinare's
   `changes_requested` branch in
   `src/coordinare/graph/nodes/monitor_performer.py:1248-1267` routes to
   `implementing` regardless of whether the relay payload is empty. An
   implementer dispatch with `relay_feedback=[]` has no information to
   act on; the inevitable result is the lifecycle re-running and the
   closer rejecting again.

4. **PR comment header doesn't distinguish reviewer from closer**.
   Both `reviewing` and `closing_review` use the same
   `"**Bot Review: {verdict}**"` header
   (`agent/performer/src/performer/main.py:1222`). A human reading
   PR #133 can't tell whether the rejecter is the bot reviewer or the
   merge-gating closer, which made the original triage harder.

**Fix** (four pieces, all on this QA-cycle branch):

- **4a — Closer prompt**: amend the `_CLOSER_PR_CHECKS_DIRECTIVE`
  in `src/coordinare/services/persona_service.py` (the directive that
  gets concatenated into the closer role prompt) to explicitly *not*
  gate on remote CI status, status
  checks, or merge-readiness. Closer evaluates code quality, thread
  resolution, and PR hygiene only. Pending or failing required checks
  are coordinare's responsibility (spec 064's PR-checks gate).

- **4b — Forward `review_body` in `changes_requested` response**.
  Add `body: str` to the reviewer/closer `PerformerResponse` payload
  for the `changes_requested` path, and surface it on the coordinare
  side as a synthetic first comment (`{"body": review_body,
  "author_login": "coordinare"}`) when the structured `comments` list is
  empty. Closer prose that explains the rejection then reaches the
  implementer (or, after 4c, the operator).

- **4c — Coordinare safety net**. In
  `src/coordinare/graph/nodes/monitor_performer.py` `changes_requested`
  branch, if `len(comments) == 0` *and* no `review_body` was forwarded
  (post-4b), do NOT route to implementer. Instead set
  `phase="blocked"`, populate
  `system_error_reason="performer reported changes_requested with no
  actionable feedback (stage=<stage>)"`, dispatch a notification, and
  return. The lifecycle stops; the operator triages.

- **4d — PR comment header attribution**. In
  `agent/performer/src/performer/main.py:1222`, use
  `"Bot Closer Review"` when `perf.role == "closing_review"`, else
  `"Bot Review"`. Three characters of prose; immense readability
  improvement on PR history.

**Tests**:

1. `tests/unit/graph/nodes/test_monitor_performer.py::test_changes_requested_with_no_actionable_feedback_blocks` —
   status with empty `comments` and empty `body` routes to
   `phase="blocked"`, not `phase="dispatching"`.
2. `tests/unit/graph/nodes/test_monitor_performer.py::test_changes_requested_with_body_only_relays_body_as_comment` —
   status with empty `comments` but non-empty `body` relays the body as
   a synthesized comment and routes to `implementing` (4b path).
3. `agent/performer/tests/unit/test_main.py::test_reviewer_changes_requested_forwards_body` —
   `PerformerResponse.body` is populated from `review_body`.
4. `agent/performer/tests/unit/test_main.py::test_closing_review_uses_distinct_pr_review_header` —
   `closing_review` posts `"**Bot Closer Review: ..."`, `reviewing`
   posts `"**Bot Review: ..."`.

## Fix 5 — Multi-card pickup dead in steady state (phase-preservation guard short-circuits Fix 2)

**Symptom**: Live testing after Fix 4 showed `active_sessions=1` every cycle
even with `max_concurrent_cards=3` and TODOs available; the daemon silently
serialised to one card again.

**Root cause**: Fix 2 fell through to TODO pickup only when bootstrap
re-entered `check_board` with no `current_card`. In steady state, the daemon
runs per-session invocations (no bootstrap) where each invocation carries
`phase="monitoring_performer"` + `current_card`. The guard at
`check_board.py:395` (`if current_phase in (..., "monitoring_performer",
...) and current_card is not None: return state`) short-circuited every
per-session invocation before the 065 US2 in_progress + TODO pickup code at
line 398+ could run. The unit tests for Fix 2 all used `initial_state()`
which defaults `phase="idle"`, so the production path was never exercised.

**Fix**:

- **5a — multi-card slot-aware phase-preservation guard**. In
  `src/coordinare/graph/nodes/check_board.py:387–410`, only return early
  from the phase-preservation guard when `max_cards <= 1` OR all
  concurrency slots are already full (counting non-`NON_SLOT_PHASES`
  sessions). When open slots remain in multi-card mode, fall through to
  the in_progress re-adopt + TODO pickup so the same cycle can fill the
  empty slots.

- **5b — preserve in-flight phase across re-adopt**. In the in_progress
  re-adopt path (~line 474–482), when the per-session invocation owns
  the current in_progress card and the incoming phase is already a
  more-specific in-flight phase (`monitoring_performer`, `dispatching`,
  `blocked`), do NOT downgrade to `monitoring_agent`. The live
  performer/dispatch state must survive the per-cycle check_board pass.

- **5c — designate one primary per cycle for TODO pickup**. To avoid N
  concurrent per-session invocations all re-running the GitHub
  side-effect paths in TODO pickup (`move_card` for cycle-blocked
  cards, dependency-unresolvable comments), only the
  lexicographically-smallest in_progress card's session falls through
  to TODO pickup; non-primary sessions return after re-adopt. Every
  concurrent session sees the same pre-fanout `in_progress` list, so
  the "primary" selection is deterministic across the fanout. The
  daemon's `new_sessions` merge dedupes by key, so the resulting
  `active_sessions` matches a bootstrap-style pickup.

**Tests**:

1. `tests/unit/graph/nodes/test_check_board.py::test_check_board_multicard_per_session_monitoring_performer_falls_through_to_todo_pickup` —
   per-session invocation with `phase="monitoring_performer"` +
   `current_card=IN_PROGRESS` + `max_concurrent_cards=3` + two TODOs
   available preserves phase/current_card AND fills the two open slots
   with the available TODOs.

## Fix 11 — Extract `service_inference` to a shared package so the performer image doesn't lose it

**Symptom**: Performer logs `coordinare_not_available` and skips service inference whenever it runs from the containerized base image. Env-cache inference is effectively dead in any deploy that doesn't ship the coordinare source tree alongside the performer.

**Root cause**: `service_inference/` lived under `src/coordinare/services/`, but the performer's Docker build context is `agent/performer/` — `coordinare` is not on PYTHONPATH inside the image. The performer guarded the import with a `try/except ImportError → "coordinare_not_available"` fallback that silently degraded.

**Fix**:

- **11a — standalone package**. Move `src/coordinare/services/service_inference/` → `packages/service_inference/src/coordinare_service_inference/` and ship it as `coordinare-service-inference` (anthropic / jinja2 / pydantic / stamina / structlog deps). It has no coordinare-internal imports, so the extraction is mechanical.
- **11b — both wheels depend on it**. Coordinare `pyproject.toml` and performer `pyproject.toml` both declare `coordinare-service-inference` and pin it via `[tool.uv.sources]` to an editable path for local dev. Imports across the repo rewrite `coordinare.services.service_inference` → `coordinare_service_inference`.
- **11c — image build reaches the package**. `Dockerfile.base` switches its build context to the repo root, COPYs `packages/service_inference/`, installs it before the performer, and strips the in-image `[tool.uv.sources]` block (the relative path doesn't exist inside the container). `bin/build`, both CI workflows, and the containerized-performer integration test follow the new context.
- **11d — fail loudly, not silently**. The `try/except ImportError → coordinare_not_available` branch in `performer/main.py` is removed. If the package is missing, the performer process must crash, not log-and-skip.

**Tests**:

- Existing `tests/unit/services/test_service_inference_*` + `tests/contract/test_env_cache_payload.py` continue to pass after the import rewrite (147 tests).
- Live verify: launch the performer against the rebuilt base image and confirm service inference runs (no `coordinare_not_available` log line).

## Fix 12 — Route `card_stuck` notifications to slack-ops

**Symptom**: `card_stuck` events (raised by the 028 phase-timeout watchdog) were logged as `notification_unrouted` and silently dropped — neither `config.yaml` nor `config.example.yaml` had a routing entry for the event type.

**Fix**: Add `event_type: card_stuck → [slack-ops]` to both config files, mirroring `card_blocked` minus the email channel (these are noisier than blocks but operators still want them visible). Document `card_stuck` in the event-type comment block in the example so the next operator doesn't repeat the omission.

## Fix 13 — Architect path accepts workspace-written `plan.md` (not just inline backend output)

**Symptom**: Card #101 architect failed with `"Backend produced an empty architecture plan"` after a 37s codex run that completed cleanly (`terminal_state=failed` raised by us, not the model). The model had written the plan via `apply_patch` tool calls into the workspace — it never emitted the plan text as an assistant message.

**Root cause**: `main.py:1055-1064` only inspects `BackendStatus.output`, which the codex backend populates from `turn/completed` assistant text (`backends/codex.py:490-518`). When the model writes the plan via workspace edits (the natural codex behaviour for "produce a file"), `output` is empty and the architect path bails before checking what's actually on disk. Bigger contexts don't fix this — the plan never travels through `output`.

**Fix**:

- **13a — detect workspace-written plan**. In the `role == "architecting"` branch, when `backend_status.output` is empty, probe `{_doc_folder(perf.score)}/plan.md` in `perf.stand.path`. If it exists and is non-empty, read it as `plan_content`; also read `{folder}/tasks.md` into `tasks_content` if present. Skip the `---TASKS---` split in this branch — the files are already separate on disk.
- **13b — idempotent commit**. Still call `commit_file(perf.stand, plan_path, plan_content, ...)` for both files. `commit_file` is already idempotent (`workspace.py:607-609`: no staged diff → no-op commit) and will push if the model wrote but did not push.
- **13c — preserve fail-loud on true emptiness**. Only return the `"Backend produced an empty architecture plan"` error when both inline `output` is empty AND no `plan.md` exists in the workspace. Log a debug field (`source="workspace"` vs `source="inline"`) on the success path so future log triage can distinguish the two routes.

**Tests**:

- New unit test in `agent/performer/tests/unit/test_main_architect.py` (or nearest existing architect test file): backend returns `BackendStatus(state="done", output=None)` but workspace has `docs/cards/101-fix/plan.md` pre-populated → assertion: returns `status="plan_committed"`, `perf.plan_path == "docs/cards/101-fix/plan.md"`, no error.
- Existing inline-output test continues to pass (backwards compatible).
- Negative test: empty output AND no workspace plan.md → still returns `status="error"` with the same `error_reason`.
- Live verify: re-run card #101 against codex; architect produces plan and the lifecycle advances to `plan_committed` without manual intervention.

## Fix 14 — CI fix loop: no-progress detection + lower max-attempt cap

**Symptom**: Live runs on ViviDynamics/website PRs #135 and #136 burned the full `CHECK_MAX_ATTEMPTS=25` budget on the same two checks failing identically every cycle (`Verify code quality`, `Validate version`). The performer never made progress but kept relaying the same failure into 25 expensive backend rounds before blocking the card.

**Root cause**: `_poll_check_runs` only blocked on `check_attempt >= CHECK_MAX_ATTEMPTS`. There was no signal that "same failure twice in a row" means the model is stuck — so a model that genuinely couldn't fix the issue burned through the entire budget instead of escalating to a human after 2–3 wasted tries. The cap was also too high; if 8 attempts can't fix it, 25 won't.

**Fix**:

- Lower `CHECK_MAX_ATTEMPTS` default from 25 → 8 in `agent/performer/src/performer/config.py`.
- Add `CHECK_NO_PROGRESS_LIMIT: int = 2` setting: bail after this many consecutive identical-failure attempts.
- Add `last_check_failure_signature: str | None` + `check_no_progress_streak: int` fields to `Performance` (in-memory only, no persistence change).
- Add `_failure_signature(failed_runs)` helper: stable hash of sorted `name|conclusion|output.title` per failing check.
- In `_poll_check_runs`'s `verdict == "fail"` branch, compute signature first; if it equals the prior one, increment streak; otherwise reset and record. When `streak >= no_progress_limit`, block the card with a `"CI checks failed with no progress across N attempts: …"` question, *before* the max-attempts check.

## Fix 15 — Tool-driven CI failure inspection (`performer-fetch-ci-log` shim)

**Symptom**: Even with Fix 14 in place, the performer's relay message only contained the Check Run's structured `output` (title/summary/text, ≤ 4 KB) — not the actual workflow logs. For real CI failures (e.g. "Validate version" on the website repo), the structured output says *what* check failed but not *why*, and the failing line lives further up in the Actions log. The model is then forced to guess.

**Root cause**: We were *pushing* a fixed amount of context at every relay (passive log-tail inlining was considered in an earlier draft of Fix 14 but is the wrong primitive — it picks the bottom 4 KB whether or not it's the relevant slice, costs a log API call on every poll, and gives the model no way to drill in).

**Fix**: Invert the flow — the relay tells the model *which* checks failed and gives it a tool. The model decides what to fetch and how much.

- New CLI shim `performer-fetch-ci-log` (`agent/performer/src/performer/cli.py::fetch_ci_log_cli`), installed via `pyproject.toml` `[project.scripts]`. End up on `$PATH` inside every performer Docker image; usable by any backend (claude_code, opencode, codex, junie, cursor) through its built-in shell tool — same pattern as `performer-upload-screenshot`.
- Modes:
  - `performer-fetch-ci-log --list` — print one line per failing check: `<job_id>\t<conclusion>\t<name>`.
  - `performer-fetch-ci-log --check '<name>' [--lines N]` — print the Check Run summary plus the tail of the Actions job log (default 4 000 chars, cap 200 000).
- Reads context from env (already populated by `Score.tool_env`): `PERFORMER_GH_TOKEN`, `PERFORMER_GH_OWNER`, `PERFORMER_GH_REPO`, `PERFORMER_GH_ISSUE` (= PR number when the role has a PR).
- New `github.get_pr_head_sha(owner, repo, pr_number, token)` helper to resolve PR → head SHA → check runs without the coordinare needing to pass the SHA into the CLI.
- Reuses existing `get_check_run_logs` (handles the 302 → S3 redirect, strips the auth header on the second hop, never raises — best-effort).
- Relay change in `_poll_check_runs`: drop the inline-log experiment and append a short tool-hint listing up to 3 failing check names as ready-to-paste commands, plus a pointer to `--list`. Exact format kept stable so the model can rely on it.
- Drop the unused `_format_check_failures_with_logs` from main.py and its `get_check_run_logs` import — the CLI shim is now the only consumer.

## Future fixes (placeholders — append as live testing surfaces them)

- US3 — kicked-back cards don't strand performers
- US4 — un-blocking restarts feedback budget (FR-012..FR-016)
- US5 — performer CI ownership (absorbs spec 043)
