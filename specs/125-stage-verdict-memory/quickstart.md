# Quickstart: Stage Verdict Memory (125)

## What changed

- Verdict stages (reviewing, security, qa, documenting, closing_review) that already issued a **passing verdict for the exact current PR head** are skipped on re-entry instead of re-dispatching a performer. The comparison is against the **live GitHub head**, so any push — performer or human — naturally re-enables dispatch.
- The documenting gate now asks "what changed since the last documentation pass?" (SHA-to-SHA compare) instead of "does the whole PR touch docs/", so tech_writer's own commits no longer re-trigger it forever.
- One PR-diff fetch per dispatch evaluation (was two on documenting).
- Issue-comment classification dedup survives restarts (persisted watermark).

No config. Snapshot schema v13 → v14 (backward compatible — older snapshots load with empty caches and today's exact behaviour). Takes effect on restart onto this build.

## Unit/contract verification

```bash
.venv/bin/pytest tests/unit/graph/nodes/test_dispatch_verdict_cache.py \
                 tests/unit/graph/nodes/test_dispatch_doc_gate_sha.py \
                 tests/unit/graph/nodes/test_monitor_performer_verdict_record.py \
                 tests/contract/test_state_persistence_v13_to_v14.py -v
.venv/bin/pytest tests/unit -q   # full regression
```

## Live verification

1. Drive a card through a passing lifecycle; watch for `monitor_performer` recording (`stage_verdicts` visible in `coordinare.state.json` after the next snapshot save).
2. Bounce the card in a way that leaves the head unchanged (e.g. a reviewer `changes_requested` the implementer disputes by replying without commits — or use an operator override to re-enter reviewing).
3. Observe `dispatch_performer.stage_skipped reason=verdict_cached` events for the already-passed stages instead of performer dispatches.
4. Push any commit → all stages dispatch again (SHA miss).
5. For the doc gate: after a tech_writer pass, bounce with a code-only commit → `stage_skipped reason=no_doc_changes_since_last_pass`; commit a `docs/` change → tech_writer dispatches.
6. Restart coordinare mid-card → `stage_verdicts` restored; issue comments processed before the restart are not re-classified (no duplicate `classify_issue_comment` calls in logs).

## Operator notes

- **Force a re-run on an unchanged head**: use the existing human-override `restart` action targeting the stage — overrides always dispatch (the cache is bypassed and then overwritten by the fresh verdict).
- A skip is always auditable: every `stage_skipped` event carries the head SHA and pairs with the recorded verdict in the snapshot.
- If GitHub is unreachable at dispatch time the cache **dispatches** (fail-open) — the feature can only save work, never skip on uncertainty.
