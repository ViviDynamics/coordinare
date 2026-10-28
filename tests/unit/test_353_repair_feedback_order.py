"""A repair's authors see the commands that failed, with their reason, before the consequences that never ran.

Dettmore e2e-06: nine checks failed on an undeclared ${SECRET_KEY_BASE} and 44 commands were not_run. The feedback
kept the first 16 non-passing results in report order, all consequences, so the c00 author never saw its nine
failures and returned an empty delta.
"""
from __future__ import annotations

import json

from coordinare.services.environment_repair import (
    FEEDBACK_MAX_BYTES,
    MAX_FEEDBACK_RESULTS,
    repair_feedback,
)


def _result(identifier, status, output=''):
    return {'id': identifier, 'status': status, 'required': True, 'output': output}


def _ids(results):
    return [item['id'] for item in repair_feedback('d' * 64, results)['results']]


def test_failures_come_before_consequences_and_keep_their_order():
    results = ([_result(f'install-{index}', 'not_run', 'prerequisite did not pass') for index in range(30)]
               + [_result('check-pass', 'pass')]
               + [_result(f'c00-{index}', 'fail', 'unresolved environment reference') for index in range(9)]
               + [_result('c01-flaky', 'indeterminate')])
    kept = _ids(results)
    assert len(kept) == MAX_FEEDBACK_RESULTS
    assert kept[:10] == [f'c00-{index}' for index in range(9)] + ['c01-flaky']
    assert kept[10:] == [f'install-{index}' for index in range(6)]


def test_one_part_with_many_failures_never_starves_another():
    results = [_result(f'c00-{index}', 'fail') for index in range(20)] + [_result('c01-only', 'fail')]
    kept = _ids(results)
    assert 'c01-only' in kept and len(kept) == MAX_FEEDBACK_RESULTS


def test_a_report_with_few_failures_keeps_them_all():
    results = [_result('a', 'not_run'), _result('b', 'fail'), _result('c', 'pass'), _result('d', 'not_run')]
    assert _ids(results) == ['b', 'a', 'd']


def test_large_failure_output_stays_within_the_byte_bound_and_keeps_each_owner():
    wide = 'é' * 512
    results = [_result(f'c00-{index}', 'fail', wide) for index in range(12)] + [_result('c01-one', 'fail', wide)]
    feedback = repair_feedback('d' * 64, results)
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert 'c01-one' in [item['id'] for item in feedback['results']]


def test_a_single_oversized_output_is_cut_to_fit():
    feedback = repair_feedback('d' * 64, [_result('c00-big', 'fail', 'é' * 20000)])
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert feedback['results'][0]['output_truncated'] is True and feedback['results'][0]['output']


def test_saved_delta_parts_from_an_earlier_brief_are_never_replayed():
    from coordinare.services import environment_shard_construction as construction

    policy = {'no_new_privileges': True}
    key = construction.delta_part_key('e' * 64, policy)
    construction.DELTA_BRIEF_VERSION, original = construction.DELTA_BRIEF_VERSION - 1, construction.DELTA_BRIEF_VERSION
    try:
        assert construction.delta_part_key('e' * 64, policy) != key
    finally:
        construction.DELTA_BRIEF_VERSION = original
