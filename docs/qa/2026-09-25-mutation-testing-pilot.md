# Mutation testing pilot (issue #439)

A mutmut 3 pilot over the six core decision services, run once at baseline and
re-runnable any time. Everything here is opt-in: no workflow runs on push or PR,
and no existing test behaviour changes.

## Scope

The six files under test are the decision surfaces whose verdicts move cards
through the pipeline, so a silent logic flip there is the most expensive class
of bug we can ship:

| Module | Lines |
| --- | --- |
| `src/coordinare/services/dispatch_guard.py` | 388 |
| `src/coordinare/services/retry_counter.py` | 271 |
| `src/coordinare/services/no_progress.py` | 93 |
| `src/coordinare/services/base_gate.py` | 127 |
| `src/coordinare/services/failure_classification.py` | 191 |
| `src/coordinare/services/required_checks_resolver.py` | 87 |

## Baseline result (2026-09-25, mutmut 3.8.0, at 1bb601b)

| Metric | Value |
| --- | --- |
| Mutants generated | 908 |
| Killed | 908 |
| Survived / timed out / no tests / skipped | 0 / 0 / 0 / 0 |
| Mutation score | **100%** |
| Wall time | ~1 min (15.56 mutations/s, forkserver isolation) |

The pilot suite kills every mutant it generates. There were no trivial
survivors to fix and no follow-up issue is needed.

Two known blind spots fall outside mutmut's model rather than inside the
pilot's reach:

- Module-level constants get no mutants at all (mutmut only mutates function
  bodies), so flipping `MAX_NO_PROGRESS_RELAYS = 2` is not scored. The
  boundary behaviour is asserted directly by `test_390_no_progress_budget.py`.
- `type_check_command` is off (see the pyproject comment), so type-level
  regressions are left to `mypy -p coordinare`, not this lane.

Sanity check that the score is not vacuous: mutating `should_block` in
`no_progress.py` by hand (`>=` → `>`, the same flip mutmut generates as
`should_block__mutmut_5`) and running the pilot selection produces exactly two
failures in `test_390_no_progress_budget.py`, and 2725 passes without the
mutation. The harness imports the mutated copies and the tests detect them.

## How to run

```sh
set -a && source .env && set +a
no_proxy='*' NO_PROXY='*' \
env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW \
  PYTHONPATH="$PWD/src:$PWD/agent/performer/src:$PWD/packages/service_inference/src:$PWD/packages/ci_detection/src" \
  .venv/bin/mutmut run
```

Results land in `mutants/mutmut-stats.json`; `mutmut results` summarises
survivors (empty output means none). `mutants/` is gitignored.

## Why the config looks the way it does

The `[tool.mutmut]` block in `pyproject.toml` encodes four lessons, each of
which cost a run to learn:

1. **`source_paths = ["src"]` with `only_mutate` restricting to the six files.**
   Pointing `source_paths` at the six files directly leaves
   `mutants/src/coordinare` without an `__init__.py`, the tests then import the
   installed (unmutated) package, and every mutant "survives" for a reason that
   has nothing to do with test quality. The whole `src` tree must be copied so
   imports resolve to the mutated copies.
2. **`pytest_add_cli_args_test_selection` is narrowed to the pilot suites.**
   The full `tests/unit` tree reads repo artefacts that don't exist inside
   `mutants/` (`.github/scripts/forward_external_issue.py`,
   `scripts/ruff_baseline.py`), so the stats phase dies partway. The pilot
   selection (`tests/unit/services`, `tests/unit/graph`,
   `test_335_watchdog_graph.py`, `test_390_no_progress_budget.py`) is
   self-contained and covers all six modules.
3. **`process_isolation = "forkserver"`.** The stats run happens in mutmut's
   own process, whose stdout is the live UI capture; a module-level structlog
   logger imported there binds the closed capture and the first test that logs
   raises "I/O operation on closed file". Forking a clean server process
   sidesteps it.
4. **`no_proxy='*'` on macOS.** In a forked child, constructing an Anthropic
   client reads macOS system proxy config via CoreFoundation, which aborts the
   process after fork (SIGABRT, exit code -6). Setting `no_proxy` makes urllib
   short-circuit to the environment before touching sysconf. Linux (CI) is not
   affected, but the var is harmless there.
5. **`type_check_command = []`.** mutmut aborts with "Could not find mutant for
   type error" when mypy reports a diagnostic it cannot attribute to a specific
   mutant (an unused `# type: ignore` at `required_checks_resolver.py:3851`,
   a missing-py.typed note at `failure_classification.py:58`). `--no-warn-unused-ignores`
   fixes the first class; nothing fixes the second. Re-enable only if the
   mypy filter is worth the maintenance.

## CI lane

`.github/workflows/mutation-testing.yml` is `workflow_dispatch` only. A full
mutmut run per PR would cost hours of runner time for a signal the unit suite
already gives on merge, so the lane exists to be triggered by hand when the
decision services or their tests change materially, and uploads
`mutants/mutmut-stats.json` as an artifact for diffing against the baseline.

## Extending the scope

To add files: add a glob to `only_mutate`, re-run, and triage survivors the
same way — a survivor is either a missing assertion (fix the test), dead code
(delete it), or an equivalent mutant (annotating `# pragma: no mutate`).
