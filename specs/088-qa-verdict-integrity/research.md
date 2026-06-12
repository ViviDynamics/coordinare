# Phase 0 Research: QA Verdict Integrity & Performer Environment Reliability

All decisions below were grounded by direct code audit (this session) — file:line refs as of `main` @ `45e1b6d`. No NEEDS CLARIFICATION items remain.

## R1 — How an evidence-less pass becomes a distinct outcome (A1, FR-001..003)

**Decision**: Introduce a third performer terminal status, `qa_env_blocked`, emitted when the QA output claims `criteria_passed > 0` (or a clean pass) while ALL evidence channels are empty (`executed_checks`, `new_tests`, validated `visual_evidence`) AND `environment_error` is set. Remove the `env_limited` early-return from `_qa_unsubstantiated_pass()` (`agent/performer/src/performer/main.py:840`) so the evidence check always runs; the env-limited case changes the *classification* (env-blocked instead of malformed), never the *exemption*.

**Rationale**: The two existing outcomes can't express "couldn't verify": `qa_failed` triggers the feedback/fix cycle (wrong — there's no code defect to fix), and `qa_passed` is the bug. A distinct status lets coordinare hold the card and repair the environment instead of either advancing a lie or burning fix-cycles. The performer-side status enum is a closed set of string literals (`main.py:2889, 2993`) and coordinare consumes it via marker sets (`monitor_performer.py:35, 67, 2020`), so the new status must be added at both ends in the same change — captured in the contract registry.

**Alternatives considered**: (a) Keep `qa_passed` + a boolean flag — rejected: every downstream consumer must remember to check the flag; that forgetting is exactly bug A3. (b) Reuse `error` — rejected: error implies harness malfunction and triggers generic retry, not env repair. (c) Map to `qa_failed` — rejected: spawns pointless fix-feedback cycles against working code (the pre-2b253aa world this project already moved away from).

## R2 — Evidence cross-validation granularity (A2, FR-007)

**Decision**: Coarse gating, not per-criterion bookkeeping: a result claiming `criteria_passed > 0` must have ≥1 item of execution evidence overall; claimed counts in the PR comment are annotated with the evidence count ("4 claimed / 2 evidence-backed") when they diverge. Per-criterion evidence mapping is NOT attempted.

**Rationale**: The QA model self-reports both sides; a per-criterion join would be fabricatable to the same degree while tripling contract complexity. Coarse gating already kills the observed failure (4/4 with zero evidence) and the annotation preserves auditability for humans.

**Alternatives**: Structured per-criterion evidence links — rejected as false precision (model-controlled on both ends); full command attestation is explicitly out of scope (spec).

## R3 — Coordinare-side env-health gate placement (A3, FR-004)

**Decision**: In `monitor_performer.py`, at the terminal-success branch (`:2031`), before `_advance_stage()`: if the same status payload carries `env_cache_health_failed`, do not advance — call the existing `mark_runtime_health_failed()` path, route the card to blocked with a structured reason (`terminal_success_env_health_failed`), and let the existing env-cache re-verify/re-bootstrap machinery clear it. The card re-runs the stage on the next pickup after the cache is healthy.

**Rationale**: The flag is already detected and logged ~1300 lines earlier (`:1729`); the bug is purely that the verdict branch never re-consults it. Holding (not failing) matches the spec's Assumption: environment problems are repairable infrastructure, not code defects.

**Alternatives**: Advance-with-annotation — rejected: downstream stages (docs/close/merge) treat advancement as trust; a tainted success would propagate exactly like today.

## R4 — App-boot evidence for visual criteria (A4, FR-005)

**Decision**: Extend the QA result contract with `app_boot` evidence: when the card's acceptance criteria include UI/visual items (detected by the existing visual-criteria handling in the qa prompt builder), the prompt requires an executed health-check command (e.g. `curl -fsS localhost:<port>` against the booted app) reported inside `executed_checks` and referenced by an `app_boot_check` field. If visual/UI criteria exist and `app_boot_check` is absent or its exit code is non-zero, those criteria are counted as unverified (folds into R1/R2 gating). Cards with no visual criteria are unaffected.

**Rationale**: Boot proof is the cheapest honest signal that screenshots were *possible*; it converts "no visual artifacts, no explanation" into either artifacts or an explicit env blocker. Reusing `executed_checks` (command + exit code) avoids a parallel evidence channel.

**Alternatives**: Performer-harness-run health check (out-of-band, not model-reported) — attractive but requires the harness to know the app's port/start command per project; deferred — the manifest/services layer doesn't model "the app" today (services-start covers redis/postgres, not the rails server). Noted as a possible follow-up spec.

## R5 — Visual-evidence link validation (A5, FR-006)

**Decision**: In the QA PR-comment builder (`main.py:2345-2394`): render a link ONLY for evidence entries whose upload returned a CDN URL; entries with local-path-only or failed upload move to the "Capture blockers / unpublished artifacts" list with the failure reason. The upload helper already returns URL-or-None; the change is in the renderer's filtering.

**Rationale**: Dead links misinform human reviewers (they imply artifacts exist). The data needed to filter is already in hand at render time.

**Alternatives**: Retry uploads — orthogonal/minor; can be added later without contract change.

## R6 — Shared backend env-merge policy (B1, FR-008)

**Decision**: New module `agent/performer/src/performer/backends/_env_policy.py` exposing `build_subprocess_env(*, cache_env, git_env, tool_env, extra=None) -> dict[str, str]` implementing exactly the #110 semantics: base `os.environ`; cache vars minus PATH; git; tool; extra; then PATH = image-PATH (fallback to standard system dirs when empty) + deduped cache-PATH dirs appended. All eight backend modules (claude_code, openclaw, opencode, opencode_compat, codex, pi, hermes, junie) call it; per-backend inline PATH logic is deleted. Backends keep their own non-env quirks (IS_SANDBOX, CLAUDE_CODE_MAX_OUTPUT_TOKENS, proxy env) layered after the helper.

**Rationale**: Six divergent policies already produced two incidents (claude_code/#110; hermes+junie latent). Append-after-image is a strict superset of strip for backends whose shells re-source activate.sh (openclaw/pi/codex/opencode): their per-command re-source wins at the front of PATH anyway, and cache-only tools become reachable even between re-sources. Junie (Node CLI) is *fixed* by this (currently full cache PATH first → startup crash class); hermes's shell tools gain the toolchain without its Go binary caring.

**Alternatives**: Keep strip for re-sourcing backends, append only for snapshotting ones — rejected: preserves divergence (the root disease) for zero behavioral benefit.

**Risk note**: opencode/opencode_compat/openclaw/pi currently STRIP cache PATH at launch; switching to append puts cache dirs at the *end* — image dirs still first, so their CLIs keep launching on image node. Conformance tests assert exactly this per backend.

## R7 — Bootstrap circuit breaker (B2, FR-009/010)

**Decision**: Extend `EnvCacheState` (and its persisted snapshot) with `bootstrap_attempts: int` and `bootstrap_exhausted: bool`, keyed implicitly by `readme_sha` (attempts reset to 0 whenever the SHA changes — existing SHA-change handling already rewrites state). Retry path (`env_cache.py:386-403`): cooldown becomes `BOOTSTRAP_RETRY_COOLDOWN_S * 2**attempts`; when `attempts >= max_attempts` (config `env_bootstrap_max_attempts`, default 3) set `bootstrap_exhausted=True`, emit `env_cache.bootstrap_exhausted` (warning + notification via the existing notify service using the existing `circuit_breaker_trip` event type), and stop dispatching. Consumer holds change their detail string to name exhaustion. Operator clear: config-reload of `env_bootstrap_max_attempts` or any SHA change resets; additionally an explicit state reset when `last_bootstrap_error` is cleared via the existing dashboard config surface is NOT built (out of scope) — documented operator action is "fix the spec files (SHA changes) or restart with the cache repaired".

**Rationale**: Escalating cooldown + cap converts the observed unbounded ~100s hammering into ≤3 attempts and one notification, while SHA-keyed reset preserves the self-healing property when someone fixes the README/Gemfile.

**Alternatives**: Time-based-only backoff without cap — rejected: still burns slots forever; the spec demands a terminal state + notification.

## R8 — Restart-resume honor path (B3, FR-011)

**Decision**: Persistence already exists (`EnvCacheStateSnapshot.last_bootstrap_succeeded`/`readme_sha`, `state_store.py:162-188`) — the defect is the honor path: a restart was observed re-bootstrapping with `last_bootstrap_succeeded=False` loaded despite a success having completed minutes earlier (success at 16:16:52; restart 17:55 loaded False). Fix in two parts: (1) **flush on completion** — `on_bootstrap_complete()` must trigger an immediate state-store save (diagnose: success outcome very likely never reached disk before shutdown; snapshot save cadence is the prime suspect — confirm in implementation with a failing test that completes a bootstrap, snapshots, reloads, and asserts True); (2) **verify-instead-of-bootstrap on restart** — when loaded state has `last_bootstrap_succeeded=True` and `readme_sha == current_sha` but the in-process `cache_dir_ready` is False (fresh boot), run the existing clean-room verifier (`daemon._verify_env_cache_clean`) instead of dispatching a bootstrap: verify pass → mark ready (consumers dispatch); verify fail → full bootstrap (phantom-success protection preserved, including the existing activate.sh-missing retrigger at `env_cache.py:405-433`).

**Rationale**: Reuses the two mechanisms that already exist (snapshot + clean verifier); only the wiring between them is missing. Meets SC-004 (<2 min: verify.sh takes ~10-20s in-container).

**Alternatives**: Trust the snapshot without re-verification — rejected: re-opens the phantom-success hole 23dd839 closed.

## R9 — Secret-refresh failure surfacing (B4, FR-012)

**Decision**: In `http_performer_service.py:418-431`: on refresh failure retry once (immediately, same token-mint path); on second failure set a session-scoped flag (`secret_refresh_failed=True`) in the session record and emit `http_performer.secret_refresh_degraded` (error level). `monitor_performer` surfaces the flag: subsequent auth-class failures of that session get the structured reason "stale credentials (refresh failed at T)" and the session is marked degraded → blocked path rather than generic error.

**Rationale**: Matches the spec's bounded-retry requirement; keeps the session running (the old token may still be valid for its remaining TTL) while guaranteeing the operator can attribute any later 401 to the refresh failure.

**Alternatives**: Kill the session on refresh failure — rejected: the refresh is proactive; the old token often outlives the job. Aborting working sessions for a maybe-problem is worse than degrade-and-attribute.

## R10 — Observability events (B5, FR-013/014)

**Decision**: (a) `workspace.py` services-start: on timeout/non-zero exit emit `env_cache.services_start_failed` at **error** level with `script`, `returncode|timeout`, `output_tail` (last 500 chars), and store the failure string on the stand so the QA flow can cite it as an environment blocker (joins `environment_error`). (b) `http_performer_service.py:469-482`: log `http_performer.job_result_malformed_json` (warning) with performer id, parse error, and `summary[:200]` before mapping to generic error.

**Rationale**: Both are pure observability adds at already-identified swallow sites; (a) additionally feeds A-cluster honesty (a services failure becomes a *visible* env blocker instead of a mystery redis connection refusal).

**Alternatives**: Failing the bootstrap/job on services-start timeout — rejected for now: some symphonies legitimately have no services; the QA evidence gate (A-cluster) now catches the downstream consequence honestly.

## Cross-cutting: deployment & compatibility

- Performer-side changes (A1/A2/A4/A5, B1, B5a) ship inside `coordinare-performer:base/full` — image rebuild required (existing `bin/build --docker` / deploy flow). Coordinare-side changes (A3, B2/B3/B4, B5b) are daemon-restart only.
- New snapshot fields default safely (`bootstrap_attempts=0`, `bootstrap_exhausted=False`) — old snapshots load unchanged (pydantic defaults).
- New status `qa_env_blocked` is emitted only by new performer images; old coordinare + new performer is avoided by deploying coordinare first (it tolerates the unknown status as non-terminal otherwise) — deployment note in quickstart.
