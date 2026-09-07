# Implementer scenario eval (spec 167)

Nine fixture cards run through the implementer workflow, one scenario each:

| fixture | work kind | milestones | expected outcome | key property |
| --- | --- | --- | --- | --- |
| `single` | feature | none (single) | pr_opened, 2 commits (test + impl) | one-turn card completes without ceremony |
| `two_milestones` | feature | 2 | pr_opened, 4 commits (test+impl per milestone) | per-milestone test-first cycle |
| `vacuous` | feature | 2 | partial_progress, no commits | tests that pass without impl trigger reprompt, then fail |
| `stuck` | feature | 1 | partial_progress, 1 commit | 3 impl attempts exhausted, red tests commit reverted |
| `ci_pending` | feature | 1 | env_blocked, 2 commits | pending checks past wait budget hold the PR |
| `bug` | bug | 1 | pr_opened, 2 commits | investigation turn before edit, note in tests and impl briefs |
| `chore` | chore | 1 | pr_opened, 1 commit | no tests turn, one change turn, baseline verified |
| `refactor` | refactor | 1 | pr_opened, 1 commit | no tests turn, one change turn, prefix is refactor |
| `tests` | tests | 1 | pr_opened, 1 commit | one tests turn, no impl turn, new tests pass against existing code |

Each fixture exercises specific gate rules and per-milestone flow constraints (SC-004):
- `single` and `two_milestones`: complete turn sequence and hand-off
- `vacuous` and `stuck`: bounding and reprompt behavior
- `ci_pending`: CI wait and environment-blocked hold (FR-014)
- `bug`: investigation gate and note carry-through (FR-021)
- `chore` and `refactor`: single-turn lanes without red check (FR-022)
- `tests`: inverted check and partial-progress on failing new test (FR-023)

Scoring checks: status, commit prefixes in order (git log main..HEAD), persona sequence
(stubbed mode only), turn caps (FR-005 + FR-006), no red pushes (SC-002: final status
not partial_progress when pushes > 0), no documentation changes (SC-003), and
phase_durations_ms present for every phase reached (FR-018).

## Stubbed (deterministic, runs under pytest)

Deterministic fake harness, fake test runner, fake git remote, fast:

```bash
cd ~/Workspaces/ViviDynamics/coordinare-167
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/implementer_scenarios -q -p no:cacheprovider
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.implementer_scenarios
```

Expected output: nine PASS results, no red in commit history.

## Live (real model through the gateway, performer image)

Real harness from `adapter.build_agent_turn_runner`, real pytest test files generated from fixtures, gateway model, performer container. Minutes per fixture. A rate to read, not a CI gate.

Run inside the performer container per quickstart.md:

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/app/src:/app/agent/performer/src:/app \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  python -m coordinare.eval.implementer_scenarios --live --only single
```

Record each fixture's duration against SC-005 (p90 under 30m for single-turn including container start and CI wait).

## Properties verified

### No red push (SC-002)

When `edges.pushes > 0`, the workflow never pushes a commit with failing tests.
Approximated in stubbed mode: `final status not partial_progress OR pushes == 0`.
Verified in live mode: git log checkout each pushed commit and run pytest.

### Bounded turns (SC-004)

Per milestone: at most 1 tests + 1 reprompt + 3 implementation.
Quality: at most 2 repairs.
CI: at most 3 repairs.
Stubbed mode: assert turn count in [min_turns, max_turns] per fixture.
Live mode: assert harness.briefs count and kinds match (no persona stubbing).

### Green CI at hand-off (SC-001)

Only `pr_opened` status when CI checks pass.
All other final statuses (partial_progress, env_blocked) mean checks failed or timed out.
Stubbed mode: edges.checks(n) returns pass for nth poll.
Live mode: wait for real checks to conclude; env_blocked if pending past budget.

### No documentation changes (SC-003)

No implementer-authored changes reach the docs/ tree.
Stubbed mode: assert not (repo / "docs").exists() or recorded as scope_revert.
Live mode: diff the PR against the baseline, verify docs/ untouched.

### Phase durations (FR-018)

Every state logs its duration_ms and outcome in the report:
- intake, plan, baseline (setup)
- milestones (per-milestone cycle)
- quality, local_gate (gates)
- push_pr (push and PR open)
- ci_wait (check polling)

Live mode: report phase durations per fixture against provisioned budgets.

## Fixture setup details

Each fixture has:

1. **Repo**: bare remote + clone on feat/x branch, baseline test (src/base.py, tests/test_base.py).
2. **Script**: dict[persona_kind -> callable] that edits files per turn kind.
   - TESTS: write test file for milestone i
   - IMPLEMENT: write src/m{i}.py
   - REPAIR_TESTS: write the same (for vacuous reprompt)
   - REPAIR_IMPLEMENT: write wrong code (for stuck cap)
   - REPAIR_QUALITY: fix lint failure
   - REPAIR_CI: fix CI failure
   - CHANGE: write config or refactored file (chore/refactor lane)
   - INVESTIGATE: return investigation note (bug lane)
3. **Edges**: fake push, open_or_update_pr, get_check_runs (with checks factory for CI results).
4. **Expect**: status, commit prefixes, persona sequence, turn range, pushed flag.

## Performance expectations (provisional budgets from FR-016)

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
