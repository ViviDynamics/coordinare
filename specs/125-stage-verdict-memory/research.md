# Research: Stage Verdict Memory (125)

## R1 — Where passing verdicts are adjudicated (recording site)

**Decision**: Record verdict entries in `monitor_performer`'s terminal-success block (`monitor_performer.py:3324-3395`), immediately before `_advance_stage(state, status)` at :3395, gated on `stage ∈ VERDICT_STAGES` and `marker == expected_marker_for(stage)`.

**Rationale**: `TERMINAL_SUCCESS_STATES` (`:44-52`) is the single funnel for passing verdicts: `approved`, `security_passed`, `qa_passed`, `docs_committed` (plus implementer/assessor markers we deliberately don't record — FR-004). The stage is authoritative from `state["performer_stage"]`; the marker↔stage map prevents a cross-wired response (e.g. a QA container replaying an `approved` marker) from recording a verdict for the wrong stage.

**Head source**: `status["head_after"]` — the 072 audit-trail settled head, reported on every terminal marker in `_TERMINAL_MARKERS_FOR_HEAD` (`:76-80`) — falling back to `status["head_sha"]`. No resolvable head → no record (fail-open toward dispatching next time). Note the accepted race: a push landing mid-review means the performer reports the newest head it observed; identical exposure exists today in GitHub's own review model (spec Edge Cases).

**Alternatives considered**: recording inside `_advance_stage` — rejected: `_advance_stage` also runs for persona-scope skips and override skips (`monitor_performer.py:264`, `dispatch_performer.py:759`), which must NOT mint verdict records (nothing was verified).

## R2 — Current-head source for the skip decision

**Decision**: `github.check_mergeability(pr_node_id)["head_ref_oid"]` fetched live at dispatch-evaluation time. Any error, empty OID, or missing `pr_node_id` → dispatch normally.

**Rationale**: The only fail-safe comparator is the *remote* head. Coordinare's own `card["head_after"]`/`head_at_last_turn` bookkeeping trails reality whenever a human pushes out-of-band; comparing recorded-SHA against local bookkeeping could false-skip exactly when a skip is most dangerous. `check_mergeability` is the established cheap GraphQL surface for this (096/097 use `head_ref_oid` for the same freshness reason, `github.py:1476-1497`).

**Cost**: one GraphQL call per verdict-stage dispatch evaluation; shared with the documenting compare gate (R4) inside the same evaluation. The 097 pre-dispatch rebase guard fetches mergeability independently on its own schedule; unifying the two fetches was considered and rejected for this feature (the guard runs in the public wrapper before the mutex; the cache check runs in the body — passing state between them couples override/mutex paths for a ~1-call saving).

## R3 — Skip decision placement and veto surfaces

**Decision**: New block in `_dispatch_performer_body` immediately after the persona-scope skip (`dispatch_performer.py:738-762`), for `performer_stage ∈ {reviewing, security, qa, documenting, closing_review}`:
skip ⇔ (a) `stage_verdicts[stage]` exists ∧ (b) `.verdict == expected_marker_for(stage)` ∧ (c) `.head_sha == live head_ref_oid` ∧ (d) `state["relay_feedback"]` empty ∧ (e) no override-forced dispatch. On skip: `_advance_stage`, log `dispatch_performer.stage_skipped` with `reason="verdict_cached"`, `card_id`, `performer_stage`, `head_sha`.

**Rationale**: This is the exact placement/pattern of the two existing skip gates (persona-scope :738, doc gate :764), which both call `_advance_stage` and return — proven not to violate the in-flight guard or mutex invariants. It runs AFTER the 097 pre-dispatch rebase guard (public wrapper), so a guard-performed rebase changes the head before the cache is consulted → natural cache miss (correct: a rebased head was never verified).

**Override veto**: `_apply_pending_override`'s `restart` action (`monitor_performer.py:269-280`) sets `performer_stage` and returns into the normal dispatch path — without a marker, the cache would skip the very stage the operator demanded. Decision: `restart` also sets a transient one-shot `state["override_forced_dispatch"] = target_stage`; the cache check consumes+clears it and dispatches. `skip` and `veto` override actions never reach the cache check (they route elsewhere). The flag is intentionally NOT persisted: a crash between override application and dispatch loses at most the *forcing* (the operator re-issues), never a verdict.

**Relay-feedback veto**: matches the 097 guard's precedent (`dispatch_performer.py:380-381`) — queued feedback means explicit work for the stage.

## R4 — Diff-between-SHAs for the documenting gate

**Decision**: Add `github.compare_changed_files(pr_url, base_sha, head_sha) -> list[str]` using REST `GET /repos/{owner}/{repo}/compare/{base}...{head}` (JSON media type, `files[].filename`), built on the same `_rest_api_base()` + `_current_token()` + httpx plumbing as `get_pr_diff` (`github.py:1153-1197`). Paginate `files` via `per_page=100` and follow `files` beyond page 1 only up to a fixed cap (3 pages ≈ 300 files); beyond the cap, treat as "compare unavailable" → fall back (a giant delta should be documented anyway, and fail-open means dispatch).

**Rationale**: FR-007 needs "paths touched between the last documented SHA and the current head". The whole-PR diff cannot express this. The compare endpoint answers it in one call without cloning. Failure modes (404 after force-push rewrote `S_doc` away, auth, network) all raise → caller falls back to the 123 whole-PR gate → then dispatch (FR-008 chain).

**Alternatives considered**: GraphQL `comparison` on repository objects — the current gql schema pin doesn't expose it cleanly for cross-SHA file lists; local `git diff` in a workspace — the coordinare host doesn't keep a checkout of performer branches.

## R5 — Shared PR-diff fetch (US3)

**Decision**: Replace the two independent fetch helpers' call sites in `_dispatch_performer_body` with one `_fetch_pr_data(state, card) -> tuple[str | None, list[str] | None]` invoked at most once per dispatch evaluation; `_should_skip_documenting` consumes `changed_files`, the `_DIFF_REVIEW_ROLES` injection consumes the sanitized raw diff. Existing helpers `_fetch_changed_files`/`_fetch_pr_diff_text` collapse into it (no other callers — verified).

**Rationale**: Verified double fetch: `dispatch_performer.py:772` (`_fetch_changed_files` → `get_pr_diff`) then `:1190` (`_fetch_pr_diff_text` → `get_pr_diff`) on the same PR within one documenting dispatch. Each discards half the tuple. Per-consumer failure semantics are preserved by returning `(None, None)` on fetch failure: gate fails open (dispatch), injection is omitted (persona instructs `gh pr diff` fallback).

## R6 — Comment watermark persistence (US4)

**Decision (corrected during implementation)**: Persist both `processed_issue_comment_ids` (as a sorted `list[int]`, bounded to the numerically largest 2000) and `last_issue_comment_id` (`int | None`) **per-card on `PersistedSession`** — the comment router reads the ACTIVE card's linked issue (`route_issue_comments.py:30-44`) and both keys already round-trip session ↔ state via `_SESSION_FIELDS` (`session.py:244-245`), so a top-level treatment would have collapsed per-card watermarks in multi-card mode. `route_issue_comments` itself is untouched.

**Rationale**: GitHub issue-comment IDs are monotonically increasing, so "numerically largest N" == "newest N" — a deterministic bound with no timestamps (FR-010). Persisting the watermark alongside the processed set also shrinks the post-restart fetch itself (`since_id` short-circuit), which is the other half of the observed restart cost.

**Alternatives considered**: top-level (WorkflowSnapshot) persistence — the original draft; rejected on code evidence: the keys are in `_SESSION_FIELDS`, i.e. genuinely per-card, and the router requires `current_card` (comments are NOT routed before card association as first assumed).

## R7 — Schema/versioning approach

**Decision**: v13 → v14. New `StageVerdict` pydantic submodel (`head_sha: str`, `verdict: str`, `recorded_at: str`) with `extra="forbid"`; `PersistedSession.stage_verdicts: dict[str, StageVerdict] = {}`; `WorkflowSnapshot` gains the two comment-watermark fields with empty defaults. No `model_validator` migration needed — pure additive defaults (the 123 v11→v12 counter migration needed one only because it *renamed* semantics). Contract test mirrors `tests/contract/test_state_persistence_v11_to_v12.py`.

**Rationale**: Follows the documented version-bump discipline in `state_store.py`'s header comment block (v10/v11/v12 precedents, backward-compatible defaults, FR-011/FR-012).
