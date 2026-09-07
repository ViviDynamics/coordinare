# Quickstart: Implementer Test-First Workflow

## Enable the workflow

In your symphony's `config.yaml` or config service, set the implementer role to use the workflow:

```yaml
performers:
  implementing:
    role: implementer
    workflow: implementer  # Enable spec 167
    workflow_env:
      QUALITY_COMMANDS: |
        npm run lint
        npm run typecheck
      TURN_WALL_CLOCK_MS: 1200000  # 20 minutes (default)
      QUALITY_REPAIR_MAX: 2        # (default)
      CI_REPAIR_MAX: 3              # (default)
      CI_WAIT_MS: 1800000          # 30 minutes (default)
```

## Run the deterministic CI eval

The five fixtures run in CI with a scripted fake harness, deterministic and fast:

```bash
cd ~/Workspaces/ViviDynamics/coordinare-167
PYTHONPATH=agent/performer/src:src python -m pytest tests/eval/implementer_scenarios/ -v
```

Expected output:
- `test_single_happy_path`: PASSED (4 commits, `pr_opened`)
- `test_two_milestones_sequence`: PASSED (6 commits, two milestones)
- `test_vacuous_tests_reprompt`: PASSED (1 reprompt turn, then fail)
- `test_stuck_impl_attempts`: PASSED (3 impl attempts, partial progress)
- `test_ci_pending_hold`: PASSED (pending checks, env_blocked)

## Run a live fixture

To run the `single` fixture live with a real model and the gateway:

1. Start the coordinare daemon and performer images. Ensure the gateway (`litellm.vividynamics.com:4000`) is reachable.

2. Inside the performer container, run:

```bash
cd /devenv/scratch/implementer-live-single
PYTHONPATH=/app/src:/app/agent/performer/src python -m pytest \
  tests/eval/implementer_scenarios/test_live.py::test_single_live \
  --live \
  --agent-model glm-5.3-flash \
  -v
```

This launches a real harness with a temporary git repository, a bare remote, and the gateway model. The run record is logged at the end.

## Properties to verify

After running fixtures (CI or live), check:

### No red push (SC-002)

```bash
# In the fixture repo:
git log --oneline
# Every commit should have passing tests at its HEAD
for sha in $(git log --oneline | awk '{print $1}'); do
  git checkout $sha
  npm test  # or detected test command
done
```

### Bounded turns (SC-004)

Check the run record in the performer logs:

```
- Single-turn card: 1 tests + 1 implement + quality + local gate + CI wait
- Two-milestone: 2×(tests + implement) + quality + local gate + CI wait
- Vacuous: 1 tests + 1 reprompt + fail (no implement)
- Stuck: 1 tests + 3 implement attempts + fail
- CI pending: all phases + pending hold
```

### Green CI at hand-off (SC-001)

The `pr_opened` status should only be reported when all CI checks pass:

```
Status: pr_opened
CI attempts: 1  # OR > 1 if repairs happened
Final CI status: pass
```

## Live testing checklist

When testing `single` or `two_milestones` live:

- [ ] Role is set to `workflow: implementer`
- [ ] Implementation brief is present (from architect, or card criteria used as fallback)
- [ ] Test command is detected (pytest, rspec, jest, etc.) OR local_test_gate is enabled
- [ ] Quality commands are declared in workflow_env (optional; lint always runs)
- [ ] PR opens without errors
- [ ] CI checks are polled (at least one check completes)
- [ ] Run record is logged at performer finish

## Performance expectations (provisional budgets from SC-005)

Single-turn card, 90th percentile:

- Container start: ~30s
- Baseline run: ~10s
- Tests turn: ~3m (including harness startup)
- Impl turn: ~4m
- Quality pass: ~2m
- Local gate: ~1m
- Push and PR open: ~30s
- CI polling (first pass): ~10m
- **Total p90: under 30m**

These budgets are provisional and will be replaced after ten live rounds are logged (SC-005).

## Debugging failures

If a fixture fails:

### Deterministic failure (CI test)

Check the test output for the run record dump. Look for:

- `status`: expected ("pr_opened", "partial_progress", etc.)
- `per_milestone[i].implementation_successful`: True or False
- `scope_reverts`: any unexpected edits the harness made
- Phase durations: any phase much slower than expected

### Live failure

Examine the performer container logs:

```bash
docker logs coordinare-performer-live 2>&1 | grep -i "workflow\|implementer\|turn\|error"
```

Check the GitHub PR for:

- Commit history (order and messages)
- Diff (out-of-scope edits)
- Check run status and logs

## Next steps after live validation

Once `single` and `two_milestones` have each been run live and the phase durations confirm the provisioned budgets:

1. Update the spec's SC-005 table with measured p90 timings.
2. Enable the workflow on a non-production symphony for further validation.
3. Roll out to the full fleet, starting with symphonies that already run `workflow: architect` (so the milestones come from a blueprint, testing the full loop).
