# Quickstart: Reviewer Workflow

## Enable the workflow

Set `workflow: reviewer` on the reviewer role in the coordinare config (config.yaml or config_override.yaml):

```yaml
backends:
  - name: reviewer-model
    backend: litellm
    model: glm-5.3-flash
    roles:
      - name: reviewing
        workflow: reviewer
```

When `workflow` is not set, the reviewing stage behaves exactly as it does today (prose path unchanged, spec FR-015).

## Run stubbed tests in CI

The deterministic eval fixtures run in CI with a stubbed model:

```bash
cd /path/to/coordinare
pytest tests/eval/reviewer_scenarios/ -v
```

All six fixtures must pass:
1. **clean**: a small PR with nothing wrong. Approved, one COMMENT review, no coverage pass.
2. **findings**: an unguarded division. Changes requested, one REQUEST_CHANGES review with one inline comment.
3. **hallucinated_anchor**: the model anchors at line 99 with invented evidence. The gate drops it, the single re-anchor call restates it at line 6, changes requested.
4. **prior_feedback**: two open prior comments, the model dispositions one. The other becomes an `unaddressed_feedback` finding anchored to the PR body.
5. **truncated**: the injected diff was cut through a third file. The coverage pass opens it; approved.
6. **docs_by_implementer**: a brief is present and `docs/usage.md` changed. A `documentation_by_implementer` finding, changes requested.

Run all fixtures (from the repo root, both source trees on the path):
```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/reviewer_scenarios -q
```

Run one fixture:
```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest "tests/eval/reviewer_scenarios/test_eval.py::test_fixture_meets_its_expectations[findings]" -q
```

The CLI runner prints one line per fixture with the verdict, finding counts and step durations:
```bash
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.reviewer_scenarios
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.reviewer_scenarios --only findings
```

## Run live eval inside the performer image

Live eval swaps the stub for the real gateway model (glm-5.3-flash via LiteLLM) and gives the survey a real temporary git repo holding the fixture's files. The GitHub poster stays a recorder in both modes: the eval never writes to a pull request. Required before rollout; a rate to read, not a CI gate.

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.reviewer_scenarios --live --only clean
```

`--live`:
- calls the LiteLLM gateway (LAN host) for the survey proposal, the findings call and any re-anchor call
- runs the admitted survey commands in the temporary repo
- records the one review the workflow would post (event, body, inline comments) without posting it
- scores against the fixture's accepted live verdicts (a real model varies) and the same anchor, coverage and single-review checks

## Verification checklist

Before merge, verify:

### Fixtures
- [ ] All six fixtures pass deterministically in CI
- [ ] `clean` fixture shows full coverage and approved verdict
- [ ] `findings` fixture shows changes_requested, one inline comment per finding
- [ ] `hallucinated_anchor` fixture shows dropped finding, re-anchor call, new findings
- [ ] `prior_feedback` fixture shows undispositioned comment added as finding
- [ ] `truncated` fixture shows coverage pass, files opened, approval or changes_requested
- [ ] `docs_by_implementer` fixture shows the documentation finding

### Live eval
- [ ] `clean` fixture runs live against real gateway, approved reported
- [ ] `findings` fixture runs live against real gateway, changes_requested reported
- [ ] Recorded review has correct event (COMMENT for clean, REQUEST_CHANGES for findings)
- [ ] Inline comments sit on correct line numbers

### Spec compliance
- [ ] SC-001: Every finding in posted reviews anchors to a line in the report
- [ ] SC-002: No approval without full coverage
- [ ] SC-003: No prior comment left undispositioned
- [ ] SC-004: Measure p90 round time on ten live rounds (target: under 10 min)
- [ ] SC-005: Implementer dispatch carries findings, repair lane selected
- [ ] SC-006: All fixtures pass deterministically

### Code quality
- [ ] Every gate rule is a pure function with its own test
- [ ] Every gate rule test fails when that rule is mutated
- [ ] No dead code in workflows/reviewer/
- [ ] No new external dependencies
- [ ] Test coverage did not regress

## Running the repair lane

After a reviewer reports changes_requested, the next implementer dispatch carries the review findings:

1. `plan.build_plan` checks `score.review_findings` first (`repair_plan`): when it carries findings, the lane is `repair` with `lane_source="review"`, whatever the brief says.
2. Milestones are the findings grouped by file path, in diff order, one milestone per path. Findings without a file anchor ride with the first group.
3. Each milestone's goal is `Address N review finding(s) in <path>`; its scope is that path.
4. Each milestone is one `REPAIR_REVIEW` turn carrying the findings verbatim (no red step), then the milestone tests against the baseline, a bounded repair on a regression, a `fix(#n): ...` commit, then the quality set.
5. After all milestones the quality set, the local gate, push, PR and the CI wait run as for every other lane.

To test the repair lane:

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/unit/workflows/implementer/test_repair_lane.py -q
```

The repair lane test:
- sets up a real temporary git repo with two source files
- injects three review findings over two files
- runs the implementer workflow with a scripted harness
- verifies one `REPAIR_REVIEW` turn per file group, in order, with no tests turn
- verifies the `fix(#7)` commits and the hand-off with the PR opened

## Debugging

If a fixture fails:

1. Check the stubbed model's output (captured in the test output). Is the JSON schema valid?
2. Check the gate step logs. Which rule failed (anchor validation, coverage, disposition)?
3. Check the workflow_metrics in the report. Did any step time out?
4. Check for unanchored findings that should have been dropped.

If live eval fails:

1. Verify the gateway is running: `curl http://localhost:8045/health` (or configured URL)
2. Check the performer container logs: `docker logs coordinare-performer:extra`
3. Verify the temp repo is correctly set up: branches, commits, diffs
4. Verify the GitHub API token is valid (for post_pull_request_review)

## Rollout gates

Before enabling `workflow: reviewer` in production:

1. All five fixtures pass deterministically in CI
2. `clean` and `findings` fixtures pass live on ten runs each
3. SC-004 measurement from live rounds confirms p90 under 10 minutes
4. SC-005 measurement confirms every implementer dispatch carries findings when expected
5. Performer images rebuilt with the new workflows/reviewer package
6. Config updated with `workflow: reviewer` on the reviewing role
7. One manual live review run to confirm end-to-end (review posts, implementer gets findings, repair lane runs)
