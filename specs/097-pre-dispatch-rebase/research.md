# Research: Pre-Dispatch Rebase Guard

## D1 — Injection point: inside `dispatch_performer`, after `check_inflight`, before `_dispatch_performer_body`

**Decision**: Place the guard in `dispatch_performer` (graph node entered when `phase=="dispatching"`), inside the per-`(card,stage)` mutex, immediately after the in-flight + multi-PR guards and right before the final `return await _dispatch_performer_body(state)` (dispatch_performer.py ~L330).

**Rationale**:
- At that point `check_inflight` has already confirmed **no performer is running** for this `(card, stage)` — so FR-004 (never rebase under a live performer) is satisfied *structurally*, with no extra check.
- `pr_url` / `current_card` are already in hand (extracted for the multi-PR check), so identifying "in-flight card with an open PR" is free.
- It is the literal "about to start a performer" moment the spec targets — rebase here and the performer always starts on a current base.

**Alternatives rejected**:
- *In `check_board` (096's site)* — that is the proactive sweep; it explicitly skips active performers and runs regardless of dispatch. The livelock is precisely that the dispatch happens there; the guard must be at the dispatch decision, not the board sweep.
- *In `_dispatch_performer_body`* — deeper than needed; the guard wants to *prevent* the body from running on a conflicting base, so it belongs before the body call.

## D2 — Reuse 096/047 machinery wholesale

**Decision**: The guard calls the existing functions, adding no new rebase logic:
- `github.check_mergeability(pr_node_id)` (096) → `mergeable_raw` (`MERGEABLE`/`CONFLICTING`/`UNKNOWN`), `merge_state_status` (`BEHIND`/…), `head_ref_oid`.
- `should_attempt_rebase(session, current_main, head)` (096) → anti-thrash gate.
- `run_rebase_round({card_id: session}, current_main, repo_url, token, …)` (047/096) → performs the rebase; returns `RebaseJob`s.
- `prepare_conflict_resolution(job, session, …)` (047) → conflict routing on `BLOCKED`.
- The per-card `last_rebase_attempt` marker (096) → written after each attempt.

**Rationale**: identical substrate to 096; this feature only changes *when* it runs (dispatch decision vs board sweep). Keeps one rebase implementation.

## D3 — Mapping the rebase outcome to a dispatch decision

**Decision**:
- **mergeable_raw ∈ {MERGEABLE/CLEAN} (current)** → no rebase; fall through to `_dispatch_performer_body` exactly as today (FR-006, SC-003).
- **CONFLICTING or BEHIND**, not thrash-guarded, head known →
  - run the single-card rebase; write `last_rebase_attempt`.
  - **CLEAN / PERFORMER_RESOLVED** → branch republished on current main; **proceed** to `_dispatch_performer_body` (performer starts on the rebased head). FR-001/FR-002.
  - **BLOCKED** → route via `prepare_conflict_resolution` and **return a held/blocked state without dispatching** (FR-003) — do not run the body.
  - **FAILED** → do not dispatch this cycle; return state unmodified so it retries; per-card isolation (the failure is caught, does not crash dispatch).
- **UNKNOWN / empty head_ref_oid** → **defer**: return state unmodified (no dispatch, no rebase) so it re-checks next cycle (FR-005).
- **thrash-guarded** (BLOCKED/FAILED against same main+head) → do not re-rebase; consistent with the card already being blocked — return without dispatching onto the known-conflicting base.

**Rationale**: a performer is dispatched **only** when the base is current (either already, or after a clean rebase). A conflicting base never receives a fresh performer (closing the livelock, FR-002/SC-002).

**Alternatives rejected**: *dispatch anyway after a failed/blocked rebase* — that is the current livelock; explicitly excluded.

## D4 — Guard applicability (when it does NOT run)

**Decision**: The guard is a no-op (dispatch unchanged) when: the card has no open published PR (e.g. the first implementer run that will *create* the branch); the branch is already current; or the auto-rebase capability is disabled (same gate as 047/096). Only an already-published, conflicting/behind branch triggers it.

**Rationale**: a brand-new card has no conflicting published branch to rebase; forcing a mergeability read there is wasted and meaningless (FR-006).

## D5 — Sourcing the current main SHA

**Decision**: Reuse `fetch_main_sha(repo_url, token)` (cached per cycle as 096 does via `_main_sha_cache`); fall back to the persisted `last_known_main_sha` only if a live fetch is unavailable. The guard needs `current_main` to pass to `run_rebase_round` and the marker.

**Rationale**: same source 096/047 use; the per-cycle cache keeps it to one `ls-remote`.

## D6 — Observability

**Decision**: Emit 096's `rebase.triggered` record with a new `reason="pre_dispatch"`, carrying `card_id`, `branch`, `prev_main_sha`, `current_main_sha`, `outcome` — secret-free (FR-010). Reuse the existing `RebaseRound` Slack/dashboard summary.

**Rationale**: one observability vocabulary across 096/097; `reason` distinguishes the trigger site for debugging.
