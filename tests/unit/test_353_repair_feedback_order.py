"""A repair's authors see the commands that failed, with their reason, before the consequences that never ran.

Dettmore e2e-06: nine checks failed on an undeclared ${SECRET_KEY_BASE} and 44 commands were not_run. The feedback
kept the first 16 non-passing results in report order, all consequences, so the c00 author never saw its nine
failures and returned an empty delta.
"""
from __future__ import annotations

from coordinare.services.environment_repair import MAX_FEEDBACK_RESULTS, repair_feedback_results


def _result(identifier, status, output=''):
    return {'id': identifier, 'status': status, 'required': True, 'output': output}


def test_failures_come_before_consequences_and_keep_their_order():
    results = ([_result(f'install-{index}', 'not_run', 'prerequisite did not pass') for index in range(30)]
               + [_result('check-pass', 'pass')]
               + [_result(f'c00-{index}', 'fail', 'unresolved environment reference') for index in range(9)]
               + [_result('c01-flaky', 'indeterminate')])
    kept = repair_feedback_results(results)
    assert len(kept) == MAX_FEEDBACK_RESULTS
    assert [item['id'] for item in kept[:10]] == [f'c00-{index}' for index in range(9)] + ['c01-flaky']
    assert [item['id'] for item in kept[10:]] == [f'install-{index}' for index in range(6)]
    assert all(item['status'] != 'pass' for item in kept)


def test_a_report_with_few_failures_keeps_them_all_in_report_order():
    results = [_result('a', 'not_run'), _result('b', 'fail'), _result('c', 'pass'), _result('d', 'not_run')]
    assert [item['id'] for item in repair_feedback_results(results)] == ['b', 'a', 'd']
