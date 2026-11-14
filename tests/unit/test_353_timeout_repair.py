"""A command the runner stopped at its timeout or the session deadline is repair feedback; other unknowns still hold.

Dettmore e2e-69 passed 34 actions, then held without a repair: rspec-full timed out at 2,700 s, seeds-spec at 900 s,
and a mock was cut short by the session deadline. Every indeterminate result refused the repair, and the refusal
carried only the generic execution_repair_held code.
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import CheckResult
from coordinare_service_inference.environment.runner import (
    COMMAND_CUT_SHORT,
    COMMAND_TIMED_OUT,
    stopped_at_limit,
)

from coordinare.services.environment_repair import prepare_repair, record_execution_failure
from tests.unit.test_353_execution_repair import (
    activation_failure_case,
    cache_fixture,
    failed_report,
    started,
)


def _stopped(cache, output: str) -> dict:
    data = failed_report(cache)
    data['results'][0].update(status='indeterminate', exit_code=None, output=output)
    return data


@pytest.mark.parametrize('marker', [COMMAND_TIMED_OUT, COMMAND_CUT_SHORT])
def test_a_command_stopped_at_a_limit_requests_a_repair(tmp_path, marker):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, _stopped(cache, f'Finished seeds\n.\n{marker}'), cleanup_confirmed=True,
                                    now=101)
    assert cache.environment_attempt['status'] == 'repair_pending'
    assert cache.environment_attempt['execution_repairs'] == 1
    _, context = prepare_repair(cache)  # the history of a run whose only failure was a stop is valid repair history
    assert context


def test_any_other_indeterminate_result_holds_with_its_own_code(tmp_path):
    # A real failure (missing activation), plus an indeterminate result the runner did not stop at a limit.
    cache, report = activation_failure_case(tmp_path)
    report['results'][0].update(status='indeterminate', exit_code=None,
                                output='output pipe remained open after process-group cleanup')
    assert not record_execution_failure(cache, report, cleanup_confirmed=True, now=101)
    journal = cache.environment_attempt
    assert journal['status'] == 'held'
    assert journal['last_error'] == 'execution_evidence_indeterminate'
    assert journal['diagnostics']['hold_message'] == 'execution repair evidence is unknown or indeterminate'
    assert journal['execution_repairs'] == 0


@pytest.mark.parametrize(('status', 'exit_code', 'output', 'expected'), [
    ('indeterminate', None, f'x\n{COMMAND_TIMED_OUT}\n', True),
    ('indeterminate', None, COMMAND_CUT_SHORT, True),
    ('indeterminate', 0, COMMAND_TIMED_OUT, False),  # a recorded exit is not the runner's stop
    ('fail', 1, COMMAND_TIMED_OUT, False),  # the command printed the words and exited
    ('indeterminate', None, f'{COMMAND_TIMED_OUT}\nthen more', False),
    ('indeterminate', None, 'background service completion requires Linux supervision', False),
    # Dettmore e2e-79: a timed-out service health step carries its service's logs after the runner's line.
    ('indeterminate', None, f'\n{COMMAND_TIMED_OUT}\n[docker logs --tail 40 db]\nready to accept connections', True),
    ('indeterminate', None, f'{COMMAND_TIMED_OUT}\n[tail of /scratch/db.log]\nlog line', True),
    ('indeterminate', None, f'pipe open\n[docker logs --tail 40 db]\n{COMMAND_TIMED_OUT}', False),
])
def test_only_the_runners_own_stop_counts(status, exit_code, output, expected):
    result = CheckResult(id='suite', status=status, required=True, exit_code=exit_code, output=output)
    assert stopped_at_limit(result) is expected
