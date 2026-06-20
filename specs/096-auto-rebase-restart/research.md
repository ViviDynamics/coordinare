# Research: Auto-Rebase Restart Resilience

## D1 — Persist the rebase baseline vs. proactively detect conflicting branches

**Decision**: Do **both**; they cover disjoint failure cases and together satisfy US1+US2.

- **Persist `last_known_main_sha`** (FR-001): restore it on startup so `check_board`'s existing edge (`current_main != prev_main → run_rebase_round`) fires on the first post-restart cycle when main advanced while the daemon was down. The first-cycle `prev_main is None → just initialize` branch becomes `prev_main = persisted value`.
- **Proactive stale/conflicting trigger** (FR-003): a persisted-baseline edge is necessary but **not sufficient**. In the live incident the daemon's *first ever* observation of website main was already post-#176, so persisted == live → no edge → branches stranded. So also trigger a rebase round when an in-flight branch is reported stale/conflicting, independent of any edge.

**Rationale**: persisted-baseline handles "main moved while down"; proactive detection handles "branch was cut from an even-older main / a missed edge / baseline already current." Neither alone fixes the observed incident.

**Alternatives rejected**:
- *Persist baseline only* — would not fix the incident (no edge when first-seen main is already current).
- *Proactive only (drop the baseline)* — loses the cheap, precise edge signal and forces a mergeable read every cycle; the baseline keeps steady state free.

## D2 — Where the startup trigger hooks

**Decision**: Restore `last_known_main_sha` during snapshot restore (so `check_board` sees it on cycle 1), and run the proactive sweep inside the **existing `check_board` rebase block** (it already has `repo_url`, `token`, `github`, `active_sessions`, and the per-cycle `_main_sha_cache`). This keeps one rebase trigger site rather than splitting logic between `daemon.py` startup and the node.

**Rationale**: `check_board` already owns the rebase trigger and all its inputs; adding the proactive branch there is the smallest, most cohesive change. Spec 094's startup-reconciliation pass runs before the first `check_board`, so the persisted baseline is in place by cycle 1.

**Alternatives rejected**: a separate startup-only rebase pass in `daemon.py` — duplicates the auth/repo-url/main-SHA plumbing already in `check_board` and creates a second code path to keep in sync.

## D3 — Anti-thrash marker (FR-007)

**Decision**: Record, per card, the `(main_sha, branch_head_sha, outcome)` of the last rebase attempt. Skip re-attempting a card whose branch head and target main are unchanged since its last recorded attempt — in particular do not re-rebase a `BLOCKED` card every cycle until either main moves or the performer pushes a new head.

- `CLEAN`/`PERFORMER_RESOLVED`: the force-push changes the branch head, so the branch becomes up-to-date and `rebase_branch` SKIPs it next round anyway — the marker mainly guards the `BLOCKED` case.
- `BLOCKED`: marker prevents a tight re-rebase loop; the card is operator-blocked per 047 until main/head changes.

**Rationale**: `run_rebase_round`/`rebase_branch` already SKIP an up-to-date branch, so clean branches don't thrash; the only thrash risk is repeatedly re-attempting an unresolvable conflict. A per-card `(main_sha, head_sha)` marker is the minimal guard.

**Alternatives rejected**: a global cooldown timer — coarser and time-based (fragile across restarts); per-card marker is deterministic and state-based.

## D4 — Reading per-PR mergeability without a new dependency

**Decision**: Use the existing `github_service` to read each in-flight PR's mergeable/aheadness state (already used elsewhere for `mergeStateStatus`/`mergeable`). Treat `CONFLICTING` or `BEHIND` as "needs rebase"; treat `UNKNOWN`/not-yet-computed as "defer, re-check next cycle" (never force-rebase on an uncomputed state).

**Rationale**: no new dependency; mirrors how mergeability is already read. Deferring on `UNKNOWN` avoids needless rebases while GitHub is still computing.

**Alternatives rejected**: a local `git merge-base --is-ancestor` aheadness probe per card — extra git operations per cycle when GitHub already exposes the signal; keep it as a possible fallback only if the API signal proves unreliable.

## D5 — Persistence shape & schema

**Decision**: Persist `last_known_main_sha` as a top-level snapshot field (it is symphony/run-global, not per-card) and the per-card anti-thrash marker on `PersistedSession` (parallel to the 090/095 per-card fields). Bump `CURRENT_SCHEMA_VERSION`; both fields default to safe empties (`None` / absent) so pre-feature snapshots load unchanged (FR-010). Values are SHAs/branch names/outcomes only — never secrets (FR-009).

**Rationale**: matches existing persistence conventions (top-level run state vs per-card `PersistedSession` fields); backward-compatible defaults follow the v8→v9 / v9→v10 precedent.

**Alternatives rejected**: storing the marker top-level keyed by card_id — duplicates what `PersistedSession` already does per card and would not round-trip with the session.

## D6 — Observability

**Decision**: Emit a structured record (`rebase.triggered` with `reason ∈ {restart_drift, proactive_conflict, main_moved}`) carrying `card_id`, `branch`, `prev_main_sha`, `current_main_sha`, `outcome` — and continue to surface the existing `RebaseRound` Slack/dashboard summary. Secret-free per FR-009.

**Rationale**: distinguishes *why* a rebase fired (restart vs edge vs conflict) for debugging the exact gap this feature closes, without duplicating the existing round summary.
