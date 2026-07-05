# Quickstart: Terminal-Success Progress Floors (126)

## What changed

- An implementer "done" after a feedback bounce is accepted only if the PR head moved past the commit the feedback was raised against, or the completion explicitly disputes items (with reasons). A no-op "done" gets one strengthened re-dispatch, then an operator hold — and never burns content-feedback budget.
- Feedback items now carry stable ids end-to-end; completions return per-item dispositions; disputes are adjudicated by the stage that raised them (or the operator, for CI-raised items) — never auto-accepted.
- A tech_writer `docs_committed` that changed nothing advances but is recorded as a no-op (it no longer mints a documentation pass, keeping 125's doc-gate baseline truthful).

No config. Snapshot schema v13 → v14 (backward compatible). Performer protocol addition is optional-field only — older performer images simply return no dispositions ("nothing disputed").

## Unit/contract verification

```bash
.venv/bin/pytest tests/unit/graph/nodes/test_success_floor.py \
                 tests/unit/graph/nodes/test_feedback_ledger.py \
                 tests/unit/graph/nodes/test_documenting_noop_completion.py \
                 tests/contract/test_state_persistence_v13_to_v14.py -v
.venv/bin/pytest tests/unit tests/contract -q   # full regression
```

## Live verification

1. Bounce a card with reviewer feedback; check the relay entries in the dispatch payload now carry `id`/`raiser`, and `feedback_origin_sha` appears in `coordinare.state.json`.
2. Have the implementer complete without committing: observe `monitor_performer.success_floor_retry` (strengthened re-dispatch naming the unaddressed ids), then on a second no-op completion `success_floor_hold` with the card in BLOCKED.
3. Have the implementer dispute an item with a reason: the card advances; the raising stage's next dispatch context contains `disputed_feedback`; its pass/bounce closes the dispute as accepted/rejected.
4. Dispute a CI-gate item: the card holds immediately (CI cannot adjudicate).
5. tech_writer completing with zero modified files: `documenting_noop_completion` event; `stage_verdicts.documenting` unchanged.

## Operator notes

- A `success_floor_hold` card names the unmoved head and outstanding item ids in its open questions — resolve by pushing the fix manually, adjusting the feedback, or overriding.
- Floors are inert on first-pass dispatches and whenever origin bookkeeping is missing (fail-open) — they only arm on stamped feedback rounds.
- Weak backends that omit dispositions but commit real work are unaffected: the floor only hard-trips on an unmoved head with nothing disputed.
