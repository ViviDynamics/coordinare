# QA scenario eval

**Do not wire this into CI.**

This suite calls a real model through the LiteLLM gateway. It is slow (minutes),
and it is nondeterministic by nature — the same scenario can produce a different
verdict on different runs.

Constitution Principle II is explicit that tests must be deterministic and that
flaky tests must be fixed or removed. Rather than dilute that principle, spec 164
keeps two separate things separate:

| | Test suite | This eval |
|---|---|---|
| Runs in CI | yes | **no** |
| Determinism | recorded model fixtures | real model |
| Purpose | catch regressions in the code | measure whether QA got *better* |
| Failure meaning | something broke | investigate a dropped rate |

`tests/eval/qa_scenarios/test_generate.py` IS part of the normal suite: the
fixture generator is ordinary code and is tested deterministically. Only the
scoring run against a live model lives outside CI.

## Running it

```bash
set -a && source .env && set +a
python -m coordinare.eval.qa_scenarios --repeats 5
```

Output is a pass rate per scenario. A scenario dropping below its previously
recorded rate is the signal to investigate; there is no threshold that fails a
build.

## Why a rate and not a pass/fail

A single sample of a nondeterministic process tells you almost nothing, and one
unlucky roll blocking unrelated work is worse than no signal. A rate tracked over
time is the only form that can answer the question the feature exists to answer:
did this change make QA better or worse?

## Adding a scenario

Add a manifest to `fixtures/` with `base_files`, `head_files`, `criteria`,
`claimed_change`, `expected_verdict` and `must_name`. A file present in
`base_files` and absent from `head_files` is deleted in the head commit, which is
how the regression scenarios express a silently removed file.

`must_name` is the "right answer for the **right reason**" assertion: substrings
the failure must reference. A scenario that fails for an unrelated reason is not
a pass.

Repositories are generated at run time. Never commit a `.git` directory.
