# Quickstart: Route the hermes (tech_writer) Backend Through the Shim

Replays the 2026-06-21 documenting block as acceptance scenarios + the activation recipe.

## Scenario A — US1: reasoning/control-char output normalized before parse (SC-001)
1. hermes routed through the normalize shim (`strip_control_chars`, `strip_reasoning`).
2. Upstream returns a body with invalid control chars / a reasoning-only answer (the live shapes).
3. **Verify:** the normalized body hermes receives is clean parseable JSON; documenting completes (no `malformed_output`). A clean body passes through unchanged.

## Scenario B — US2: completion probe admits hermes; gates a broken upstream (SC-002)
1. hermes target with `health_probe: completion`.
2. Normal completion → healthy → proceed. Empty/non-200/timeout (the observed overload shape) → unhealthy → fail_closed (or reroute if declared).
3. **Verify:** 0 false-negative "no tool call" rejections; 0 fail-open admissions.

## Scenario C — US3: correct wire path (SC-003)
1. hermes routed; loopback base = `<loopback>/v1`.
2. **Verify:** hermes's appended `/chat/completions` hits the served `/v1/chat/completions` route (no 404); a misaddressed base surfaces unhealthy at the startup probe.

## Scenario D — default-safe (SC-004)
1. No hermes routing entry.
2. **Verify:** hermes talks its configured provider directly (unchanged); openclaw/junie/claude/qa paths identical to before.

## Scenario E — activation recipe (config/ops, enabled by this spec)
1. Add to `routing.yaml`:
   ```yaml
   - backend: hermes
     model: gpt-oss:120b
     target:
       base_url: http://192.168.3.30:11434  # bare Ollama origin, no auth (probe sends none)
       wire_format: openai
       strategy: normalize
       health_probe: completion
       normalizers: [strip_control_chars, strip_reasoning]
   ```
2. Mount `routing.yaml` into `hermes-ephemeral` + set `SELFHOSTED_ROUTING_CONFIG` (currently not mounted there).
3. Rebuild the performer image (`bin/build --docker` → `:full`+`:extra`) so the launch-eligibility change ships.
4. Restart coordinare.
5. **Verify:** the documenting stage on a flaky gpt-oss upstream is normalized and completes; a persistently empty upstream gates as infra, not a card fault.
