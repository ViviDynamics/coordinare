# Research: Terminal-Success Progress Floors (126)

## R1 — Where success is trusted today (the gap)

**Decision**: Add the floor inside monitor_performer's implementing terminal-success branch, after the `env_cache_health_failed` taint check and before the 075 CI gate.

**Rationale**: Verified paths: `TERMINAL_SUCCESS_STATES` advance via `_advance_stage` with no head validation; the 070 guard (implementer) and 072 guard (`ZERO_PROGRESS_REVIEW_STAGES`, `monitor_performer.py:4298-4340`) fire only on `blocked`/`partial_progress` markers — success markers bypass both. Running the floor before the CI gate avoids spending a CI evaluation on a completion that changed nothing.

**Alternatives considered**: floor inside `_advance_stage` — rejected (also runs for skip paths); floor in dispatch (next cycle) — rejected (loses the `status` payload carrying dispositions).

## R2 — Progress reference: feedback-origin SHA

**Decision**: Stamp `feedback_origin_sha` at bounce time — the head the bouncing verdict was issued against — and compare the completion's `head_after` against it. Sources: the CI gate's rollup head (already tracked for `bounce_counter` keying), and the review-verdict handlers' `status.head_after`/`head_sha`.

**Rationale**: Comparing against dispatch-time head (`head_at_dispatch`) would flag "already fixed by a prior crashed session's commits" as no-progress — the spec's Edge Cases explicitly require those commits to count. The 2026-07-03 review's verifier analysis rejected diff/line-range matching of feedback to commits (line numbers go stale); a SHA floor plus explicit dispositions is the deterministic alternative.

## R3 — Ledger + wire shape

**Decision**: One per-card `feedback_ledger` (persisted): records `{id, raiser, origin_sha, body_digest, disposition, dispute_reason, re_raised, round_status}`. relay_feedback entries delivered to the implementer gain `id`, `raiser`, `re_raised` keys inline; the completion returns `feedback_dispositions: [{id, disposition, reason?}]` on `ProtocolResponse`.

**Rationale**: `relay_feedback` is already `list[dict]` end-to-end (dispatch registry line 49; performer `Score.relay_feedback` `models.py:117`; pydantic inner dicts unconstrained) — no wire-schema break. The response field is additive on both `ProtocolResponse` models; absent field ⇒ empty ⇒ "nothing disputed", which is exactly the backward-compatible meaning. Bodies are digested (first 200 chars) in the ledger to bound snapshot growth; full bodies live only in the transient relay entries.

**Alternatives considered**: item-level identity via body hashing for re-raise matching — rejected as fuzzy; re-raise tracking is per-raiser round (plan, Scale/Scope note).

## R4 — Floor semantics with weak backends

**Decision**: The floor hard-trips only when BOTH (a) head unchanged vs `feedback_origin_sha` AND (b) zero disputed dispositions. Missing dispositions with a moved head ⇒ accepted (items ride forward; the raising stage re-raises if they mattered — spec Edge Cases "mixed completions").

**Rationale**: Several live backends (junie, gpt-oss variants) have contract-compliance gaps (098/119 history); making dispositions mandatory-for-acceptance would convert every weak-model completion into a floor trip. The floor's purpose is stopping *no-op* thrash, not enforcing paperwork.

## R5 — Trip handling and budgets

**Decision**: First trip ⇒ strengthened re-dispatch reusing the 070/072 shape (`relay_feedback` = unaddressed items + directive naming them; same-stage re-dispatch; `noop_success_retries += 1`). Second trip (counter ≥1) ⇒ `phase="blocked"` hold with `open_questions` naming the unmoved head and outstanding item ids, routed through the existing handle_blocked/notify surfaces. Reset `noop_success_retries` whenever the head moves or a new feedback round is stamped. Neither `content_feedback_cycles` nor `transient_error_cycles` is touched (FR-003) — trips are not new content feedback and not infra.

## R6 — Dispute adjudication + FR-012 interplay with 125

**Decision**: `disputed` entries queue for their raiser. Dispatch injects `card_context["disputed_feedback"]` (id, body, reason) into that stage's next run. Stage passes ⇒ round accepted (entries closed). Stage bounces ⇒ round rejected: new items stamped `re_raised=true`; a subsequent head-unmoved dispute against a re-raised round ⇒ hold. `raiser="ci"` items (CI-gate bounces) skip adjudication — a dispute of a red check routes straight to hold (FR-007). 125's `_verdict_cache_check` gains rule V2b: pending disputed entries for a stage ⇒ dispatch (a cached verdict must never silently swallow a dispute; also naturally true today only when the head moved).

**Rationale**: The raiser is the only party with standing to withdraw its demand; coordinare never auto-accepts (SC-004). Stage-level rounds keep the state machine deterministic without semantic matching.

## R7 — US3 no-op documentation completions

**Decision**: In the documenting terminal-success path: `docs_committed` with empty `files_modified` AND `head_after == head_at_dispatch` (or no head delta signal) ⇒ advance the stage, emit `documenting_noop_completion`, and suppress 125's `_record_stage_verdict` for this completion.

**Rationale**: The 2026-07-03 review verifier flagged conflating `files_modified` (this session's commits) with PR `changed_files` — a tech_writer inheriting current docs legitimately commits nothing. Advancing (never bouncing) keeps the honest case cheap; suppressing the verdict record keeps 125's last-documented SHA meaning "docs were actually produced at this head", so the doc gate's compare baseline stays truthful. Verdict-cache skip for documenting still requires an exact live-head match of a REAL pass — a no-op never mints one.

## R8 — Schema/versioning

**Decision**: v13 → v14: `feedback_ledger: list[FeedbackItemRecord]` (tolerant per-entry validation, mirroring `repair_audit`/`StageVerdict` handling), `feedback_origin_sha: str | None`, `noop_success_retries: int`. Ledger pruned at stamp time to the current + immediately-previous round. Contract test mirrors v12→v13. Registry updates in `specs/contracts/dispatch-payload.md`: relay_feedback entry keys (`id`, `raiser`, `re_raised`), new `disputed_feedback` context field, and the `feedback_dispositions` response field (response contract section).
