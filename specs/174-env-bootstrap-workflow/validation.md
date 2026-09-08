# Validation record

- Spec analysis ran before implementation: seven requirements mapped to six tasks,
  no unresolved contract or scope findings. Initial workflow test collection failed
  because the workflow did not yet exist.
- Production Toolkit/adapter and dispatch fixtures: 29 passed. They use the actual
  adapter, real git repositories, real shell verification and process cancellation;
  only the external install harness and service inference are fixture-controlled.
- Focused performer main/workspace/readiness tests: 340 passed before the final
  additional strict-report cases; those six strict-report cases also pass.
- First full performer run: 1519 passed, including Docker integration.
- First full coordinare run: 7970 passed, 13 existing skips, 99 default marker
  deselections. The coverage result was 89.30%, but review edits overlapped that
  run, so final stable-source coverage must be rerun before merge. Issue #286
  independently repairs build setup and precise coverage-floor enforcement.
- All 26 named real-source mutations were killed by their specified regression
  tests. Every restoration used saved bytes and verified SHA256. See
  mutation-results.txt; mutations.py is reproducible with the same Python runtime.
- Independent adversarial review round one reproduced four findings: service gate
  cancellation cleanup, malformed terminal report handling, cancelled-turn metrics,
  and symphony-specific workflow resolution. All were fixed and verified by execution
  in round two. No remaining concrete blockers were found. This is an explicit
  independent review substitution; no unavailable /review skill is claimed.
- Ruff across src, tests and agent/performer passes. No production configuration
  was changed, and no live model traffic was needed for the deterministic seam eval.

Final build, current-head GitHub CI, merge and main-image verification are recorded
on PR #289 before completion of T006.

- Copilot review on b208d6e found an invalid-timeout dispatch exception. The fix preserves the invalid value for the workflow to reject before installation; dispatch regression passes and named mutation invalid_dispatch_timeout is killed with verified source restoration.
- Final local browser suite: 196 passed across Chromium and Firefox.
- Final performer run: 1520 passed; one full-image build hit its 300-second timeout. The following full-image capability test passed, and an isolated rerun of the timed-out test passed in 25.33 seconds.
