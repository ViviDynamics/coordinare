# Quickstart: Security Workflow

## Enable

```yaml
performers:
  security:
    workflow: security
    workflow_env:
      SECURITY_SEMGREP_CONFIG: "auto"    # or p/default, or a rules directory, when the container has no registry access
      SECURITY_SCAN_TIMEOUT_S: "120"
```

Remove the `workflow` line to restore the prose path exactly, including coordinare's spec-083 dispatch-time scan.

## Run the stubbed eval (deterministic, runs in CI)

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/security_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.security_scenarios
```

Six fixtures must pass:

1. **clean**: nothing blocking, scanners clean. `security_passed`, one COMMENT review.
2. **injection**: a request parameter concatenated into SQL. One `injection` finding, high, implementer, inline; `security_failed`.
3. **secret**: the fake scanner reports a committed key, the model reports nothing. Critical, `tool=semgrep`, `security_failed`.
4. **scanner_unavailable**: the fake runner reports semgrep missing. `env_blocked` naming semgrep, no model call, no review.
5. **advisory_only**: weak hashing. `security_passed`, one COMMENT whose body names the `weak_crypto` advisory.
6. **downgrade**: an `injection` finding with a `downgrade_reason`. Advisory, `downgraded=true`, `security_passed`, the body names the downgrade.

## Run live inside the performer image

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.security_scenarios --live --only injection
```

`--live` uses the gateway model, the real semgrep and bandit in the image over a temporary repository holding the fixture, and a recording poster: nothing is written to GitHub.

## What coordinare does after the round

- `security_passed`: the stage advances as today.
- `security_failed`: the 022 routing block sends implementer-routed findings to the implementer and architect-routed findings to the architect; the blocking implementer-routed findings are also on the card as `review_findings`, so the next implementing dispatch runs the spec-167 repair lane.
- `env_blocked`: the card holds with the tool or the unread files named; nothing is lifted.

## Verification checklist

- [ ] Six fixtures pass in CI; `clean` and `injection` pass live.
- [ ] Every gate rule test fails under its named mutation (table in the PR).
- [ ] Parity test: the performer normaliser equals coordinare's over the shared fixtures.
- [ ] Without `workflow: security`: the 083 floor runs at dispatch and the monitor merge applies (tests both ways).
- [ ] The prose security path in main.py is unchanged byte for byte.
- [ ] Both trees green in separate invocations; coverage gate holds.
