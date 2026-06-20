# Quickstart: Completion-Style Health-Probe Mode

Replays the 2026-06-20 098-US2-activation block as acceptance scenarios + the
junie activation recipe this feature enables.

## Scenario A — US1: completion-mode target admitted on a normal completion (SC-001)
1. A target declares `health_probe: completion`, strategy `normalize`.
2. Upstream returns a normal non-empty completion.
3. **Verify:** healthy → proceed. (Today, with the tool-call probe, the same target gates unhealthy → fail_closed.)

## Scenario B — US1: completion-mode fail-closed on empty/broken upstream (SC-003)
1. Same target; upstream returns empty content / non-200 / times out.
2. **Verify:** unhealthy → fail_closed (or rerouted if `reroute_upstream` declared). Never admitted.

## Scenario C — US1: normalize-then-judge (FR-004)
1. Completion-mode target whose upstream returns an empty answer with a populated reasoning channel; normalizers include `strip_reasoning`.
2. **Verify:** the probe judges the NORMALIZED result (reasoning promoted → non-empty content) → healthy.

## Scenario D — US2: existing tool-call targets unchanged (SC-002)
1. The reviewer/qa targets (no `health_probe` declared).
2. **Verify:** identical health decision (healthy/unhealthy/rerouted/fail_closed) to before this feature.

## Scenario E — config fail-fast (SC-006)
1. A routing entry declares `health_probe: bogus`.
2. **Verify:** config-load fails with a clear error (no silent default-through).

## Scenario F — US3 / activation: junie assessor routed with 098 normalization (SC-004)
The recipe this feature enables (operator/ops step, not code in this spec):
1. Add to `routing.yaml`:
   ```yaml
   - backend: junie
     model: gpt-oss:120b
     target:
       base_url: http://192.168.3.30:11434
       wire_format: openai
       strategy: normalize
       health_probe: completion
       normalizers: [strip_control_chars, strip_reasoning]
   ```
2. Mount `routing.yaml` into `junie-ephemeral` (config.yaml) + set `SELFHOSTED_ROUTING_CONFIG` (currently only on claude-/openclaw-ephemeral).
3. Rebuild the performer image (`bin/build --all`) so the 098 control-char normalizer is in the container.
4. Restart coordinare.
5. **Verify:** the assessor is admitted on a normal completion; a control-char / empty-reasoned upstream is repaired before junie's parser (098 US2); a persistently empty upstream gates unhealthy (infra), not a card-fault.
