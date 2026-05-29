# Quickstart — QA Cycle 076 Operator Verification

**Spec:** [spec.md](./spec.md) | **Plan:** [plan.md](./plan.md)

How to verify the 076 dispatcher fix in a live environment, and how to recognise the new structured-log events that surface the new behaviour.

---

## TL;DR

After 076 lands, restarting coordinare mid-flight is **safe**: any in-flight performer container is either re-adopted into the new daemon's registry or cleanly reaped before a replacement is launched. You will never again see two performer containers competing for the same card. You will also never again see a card stuck in `IN_PROGRESS` with `active_card` pinned but no session — the wedge invariant catches that and releases the pin.

---

## Before vs. After

Each row references an anomaly from the 2026-05-28 live-test incident.

| Anomaly | Before 076 | After 076 |
|---|---|---|
| **Duplicate dispatch** (two containers, same card, 51 s apart) | Two `coordinare-performer:full` containers running, both burning tokens, coordinare tracks only one | At most one container per `(card_id, performer_stage)`. Reconciliation pass adopts the existing one on restart; in-flight guard refuses duplicate dispatches mid-run |
| **Successful turn forgotten** (PR #148 opened, coordinare still on PR #133) | `state.active_card.pr_url` stays on the stale PR; downstream stages evaluate the wrong head SHA | PR fields update within the same poll cycle that processes the success; snapshot reflects the new PR before any next dispatch |
| **Lifecycle stranding** (DONE → another implementer dispatched) | Implementer success leads to a second implementer dispatch instead of advancing to reviewer | Every terminal outcome (DONE/PARTIAL/BLOCKED/IDLE_TIMEOUT) causes an explicit recorded transition; "DONE → same stage again" is forbidden |
| **Branch forking** (two open PRs on one card) | Implementer makes up its own branch name → competing PR | Dispatcher injects the canonical branch into the performer's input; performer is contractually required to push to it; mismatched branches surface as a contract violation |
| **Board ↔ state divergence** (board=TODO, coordinare=IN_PROGRESS-pinned) | Card wedged, coordinare idle, no recovery | Per-cycle invariant detects the divergence and releases the pin (default per clarification Q1); next cycle re-picks normally |
| **Orphan container** (compassionate_meitner, claude exited, container still up) | Container lingers forever; operator must `docker stop` manually | Startup reconciliation sweeps unreferenced containers; mid-run drain+reap handles relay handoffs |
| **qwen 10-min idle** (model produces zero output) | Container is killed, but card has no recorded retry → can loop infinitely on restart | Retry counter persists across restarts; 2 retries per `(card, stage)` per 24 h, then BLOCKED |

---

## Step 1 — Confirm 076 is live

```bash
# Check the daemon version exposed via health endpoint (added in 076)
curl -s http://localhost:9090/health | jq -r '.spec_versions // .version'
# Look for "076" in the response.

# Check the new config block is parsed
.venv/bin/python -c "
import yaml, pathlib, os
from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
raw = yaml.safe_load(os.path.expandvars(pathlib.Path('config.yaml').read_text()))
cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
print('dispatcher_dedup:', cfg.dispatcher_dedup)
"
# Expect a populated DispatcherDedupConfig with defaults from data-model.md section 9.
```

---

## Step 2 — Verify Docker labels are applied on new dispatches

```bash
# After coordinare dispatches a performer, inspect the container's labels.
CID=$(docker ps --filter ancestor=coordinare-performer:full --format '{{.ID}}' | head -1)
docker inspect "$CID" --format '{{json .Config.Labels}}' | jq .
# Expect ALL 6 labels:
# coordinare.performer.id, coordinare.session_id, coordinare.card_id,
# coordinare.performer_stage, coordinare.daemon_started_at, coordinare.spec_version="076"
```

If any of the 5 new labels is missing on a container launched by coordinare running 076, that is a bug — file a finding.

---

## Step 3 — Reconciliation in action (the FR-014 regression scenario)

Reproduce today's incident pattern; the fix should now make it benign.

```bash
# 1. Start coordinare with at least one in-flight card
bin/start --env .env --config config.claude.yaml &
# Wait for it to dispatch a performer (watch for "performer_endpoint.transition busy")

# 2. SIGTERM coordinare
pkill -TERM -f "python -m coordinare"

# 3. Wait for the performer container to remain (it survives the daemon)
docker ps --filter ancestor=coordinare-performer:full

# 4. Relaunch coordinare
bin/start --env .env --config config.claude.yaml &

# 5. Within 30 seconds, check the logs for the reconciliation pass
grep -E 'daemon\.reconciliation_pass_' /path/to/coordinare.log | tail -20
# Expect:
#   daemon.reconciliation_pass_started cards_to_process=1 budget_seconds=30.0
#   daemon.reconciliation_pass_complete wall_clock_seconds=0.X decisions={...:adopted}

# 6. Confirm exactly one container per card
docker ps --filter ancestor=coordinare-performer:full \
  --format '{{.Names}}\t{{.Labels}}' \
  | grep "coordinare.card_id=$CARD_NODE_ID" | wc -l
# Expect: 1
```

If step 6 returns 0, reconciliation reaped instead of adopting — check `daemon.reap_failed` events for why.

If step 6 returns 2 or more, the fix has regressed. File a finding immediately and consult `data-model.md` §2 (ReconciliationDecision) for which decision path mis-fired.

---

## Step 4 — Wedge invariant in action (FR-020)

```bash
# Synthetic test: manually edit the snapshot to wedge a card, then start coordinare
# (DO THIS ONLY ON A DEV COORDINARE; never on a production state file)

# 1. Stop coordinare cleanly
# 2. Edit the snapshot to set state.active_card.id = X but state.active_sessions = {}
#    and state.phase = null
# 3. Start coordinare
bin/start --env .env --config config.claude.yaml &

# 4. Within one poll cycle (≤30s), check logs
grep 'daemon.wedge' /path/to/coordinare.log | tail -5
# Expect:
#   daemon.wedged_state_detected card_id=X phase=null performer_stage=null
#   daemon.wedge_resolution card_id=X resolution=released
```

---

## Step 5 — Multi-PR detection (FR-024)

```bash
# Force the divergence: manually open a second coordinare/<card_node_id>/<other-slug> PR
# on the same card's repo (simulating yesterday's anomaly).

# Then trigger a dispatch — the in-flight guard / canonical-branch resolver should detect:
grep 'multi_pr_divergence' /path/to/coordinare.log | tail -5
# Expect:
#   daemon.multi_pr_divergence_detected card_id=X pr_numbers=[133, 148] canonical_branch_prefix=coordinare/<cid>/
#   dispatch_performer.multi_pr_divergence_refused card_id=X
# A card_blocked Slack notification should also fire.
```

---

## Step 6 — Idle-timeout retry counter (FR-019)

```bash
# Simulate a stalling model (or wait for one to occur naturally with qwen):
grep 'monitor_performer.idle_timeout' /path/to/coordinare.log | tail -10
# Expect a sequence like:
#   monitor_performer.idle_timeout card_id=X stage=implementing attempt=1 budget=2
#   monitor_performer.idle_timeout card_id=X stage=implementing attempt=2 budget=2
#   monitor_performer.idle_timeout_exhausted card_id=X stage=implementing attempts=2
#   card_blocked card_id=X reason=idle_timeout_exhausted
```

The retry counter survives daemon restart — verify by restarting between attempts and confirming `attempt` continues from where it left off.

---

## New structured-log events introduced by 076

Grep recipes for the dashboard / operator audit trail:

```bash
# Reconciliation pass
grep 'daemon\.reconciliation_pass_' coordinare.log

# In-flight guard
grep 'dispatch_performer\.in_flight_guard_' coordinare.log

# Wedge invariant
grep 'daemon\.wedge' coordinare.log

# Multi-PR divergence
grep 'multi_pr_divergence' coordinare.log

# Branch-contract violations
grep 'branch_contract_violated' coordinare.log

# Idle-timeout retry counter
grep 'monitor_performer\.idle_timeout' coordinare.log

# Notification dedup decisions
grep 'notify\.card_dispatched_' coordinare.log
```

---

## Snapshot schema migration (v6 → v7)

076 bumps the snapshot schema version. Existing v6 snapshots load cleanly with new fields defaulted to empty values. No manual migration step is required.

If you need to roll back to a pre-076 coordinare: snapshots written by 076 (v7) cannot be loaded by pre-076 coordinare (which only knows up to v6). Have a v6 snapshot backup before adopting 076 in any environment you can't afford to lose state in.

---

## What's NOT covered by 076

- The `spark/qwen3.6:35b` 10-minute idle stall is documented as a model-tier issue, not a code bug. See `project_qwen_coder_limitations.md`. The 076 fix bounds the damage (2 retries → BLOCKED) but does not prevent the stall itself.
- Cross-host / distributed coordinare coordination is out of scope. 076 assumes one coordinare process per host.
- Resolution mechanism for an already-divergent multi-PR state: 076 detects and blocks new dispatches, but does not automatically close one PR — that is an operator decision.
