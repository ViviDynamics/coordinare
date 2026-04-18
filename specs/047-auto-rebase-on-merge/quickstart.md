# Quickstart: Auto-Rebase Active Branches on Merge

## Scenario 1 — Clean rebase after sibling merge (P1)

### Setup
1. Start coordinare with `max_concurrent_cards: 2`
2. Create two issues (#201 "Feature A", #202 "Feature B") on the board in TODO
3. Let both progress through the lifecycle until both have open PRs on separate branches
4. Let card A's lifecycle complete — closer merges PR to main

### Expected behavior
1. merge_pr stores the new main SHA as `last_known_main_sha`
2. Coordinare identifies card B's session — branch is behind the new main
3. Rebase service clones repo, fetches main + card B's branch
4. `git rebase origin/main` succeeds (no conflicts — different files)
5. Force-push-with-lease succeeds
6. Card B's lifecycle stage unchanged (still in reviewing/monitoring_pr)
7. CI re-runs on the updated branch

### Verify
- Coordinare log: `rebase.clean` with card_id, branch, new_sha
- Slack: single summary "Rebase round: 1 branch rebased cleanly"
- Dashboard: card B shows rebase status "clean" with new SHA

---

## Scenario 2 — Performer resolves conflict (P2)

### Setup
1. Two PRs that edit the same file (e.g., both add imports to the same file)
2. Merge card A's PR

### Expected behavior
1. `git rebase origin/main` on card B fails with conflict
2. Coordinare reads conflict markers, dispatches performer with relay_feedback
3. Performer resolves the import ordering conflict, `git add` + `git rebase --continue`
4. Force-push-with-lease succeeds
5. Card B's lifecycle continues

### Verify
- Coordinare log: `rebase.conflict_detected` → `rebase.performer_dispatched` → `rebase.performer_resolved`
- Slack summary: "branch B conflict resolved by performer"
- Card B's PR shows force-pushed commits with the resolved merge

---

## Scenario 3 — Unresolvable conflict blocks card (P2)

### Setup
1. Two PRs that change the same function signature incompatibly
2. Merge card A's PR

### Expected behavior
1. `git rebase origin/main` on card B fails with conflict
2. Performer dispatched, reads conflict, determines it's semantically ambiguous
3. Performer returns error/blocked
4. Card B moved to BLOCKED with diagnostic comment listing conflicted files + content preview
5. @-mention of human_reviewers in the comment

### Verify
- GitHub issue comment on card B: lists conflicted file paths, shows truncated conflict content
- Slack: "branch B blocked — conflict in `src/app.py`"
- Dashboard: card B shows "blocked — rebase conflict"

---

## Scenario 4 — External merge detected (non-coordinare PR)

### Setup
1. Card A is in-flight with an open PR
2. A human merges a different PR to main (not managed by coordinare)

### Expected behavior
1. Next poll cycle: coordinare fetches main HEAD, compares to `last_known_main_sha`
2. Detects change → triggers rebase round for card A
3. Same clean/conflict/block flow as scenarios 1-3

### Verify
- Coordinare log: `check_board.main_head_changed` with old_sha, new_sha
- Rebase fires even though no coordinare-initiated merge occurred

---

## Scenario 5 — Active performer skipped (FR-006)

### Setup
1. Card B is in `monitoring_performer` phase (implementer actively running)
2. Card A's PR merges to main

### Expected behavior
1. Rebase round starts for card B
2. Active-session guard detects card B's phase is `monitoring_performer`
3. Card B's branch is SKIPPED for this round
4. After card B's performer completes, next rebase round picks it up

### Verify
- Coordinare log: `rebase.skipped_active_performer` with card_id
- Slack summary: "1 branch skipped (active performer)"
- Card B's branch is rebased on the next round after the performer finishes
