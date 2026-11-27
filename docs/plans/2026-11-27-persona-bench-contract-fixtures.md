# persona bench: payloads and fixtures behind the role contracts; QA findings lost

Issue #391

## Scope
In:
- The bench builds every `/jobs` payload with the production builders in
  `http_performer_service` (now module functions), from a card context shaped
  like the one `dispatch_performer`, the env-cache bootstrap and the intake
  dispatch build. That brings the security scan request, the PR diff context,
  the reviewed environment manifest and assessment, the nare rail metadata and
  the role workflow to the bench.
- `env_bootstrap` runs production's environment discovery and independent
  review on the fixture repository, then dispatches with the accepted contract
  and a cache volume.
- The two tech_writer tasks get distinct labels; the `feat/techwriter-wiki`
  fixture branch exists in the fixture repository.
- Every cell saves the performer's job transcript (the raw backend output)
  beside its cell JSON.
- An advocate task: per-run fixture issues, a grader over the run's outcome
  record, a judge rubric, and the card-less intake payload.
- QA: string `failures` entries are kept as failures, executed checks accept
  common field spellings, and the contract reminder and persona state the exact
  shapes. The unsubstantiated-pass guard is unchanged.

Out:
- Running the bench against a live model (needs a model key; done by the
  operator afterwards).
- Cleaning the stale `bench-*` branches already in the fixture repository.

## Assumptions
- A string failure is a reported defect: keeping it can only fail a verdict,
  never pass one, so the evidence guard is not weakened.
- An executed check still needs a command and an exit code; only the key
  spellings widen (`cmd`, `returncode`, `exitCode`, and similar).
- The advocate fixture issues are created per run and closed afterwards, so the
  advocate's handled label never hides them from a later run.

## Tasks
- [x] 1. Fixture branch `feat/techwriter-wiki` pushed to the fixture repository.
- [x] 2. Production payload builders are module functions: a test calls them
  without a service instance.
- [ ] 3. Bench card context and payload go through the production builder: tests
  for nare rail metadata, security scan request, PR diff context.
- [ ] 4. env_bootstrap card context from discovery and review: test with a fake
  chat reaches an accepted manifest the performer's contract check accepts.
- [ ] 5. Distinct tech_writer labels, `--roles` matches label or performer key.
- [ ] 6. Transcript saved per cell: test the cursor loop and the file.
- [ ] 7. Advocate task: payload, grader and fixture-issue lifecycle tests.
- [x] 8. QA failures and executed checks: tests for string failures and aliased
  check keys, and that the guard still refuses a check with no exit code.
- [x] 9. Contract reminder and persona state the exact shapes.
