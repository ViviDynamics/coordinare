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


def test_byte_trimming_shortens_outputs_before_dropping_any_owner():
    emoji = '\U0001F600' * 512
    results = [_result(f'c0{index}-check', 'fail', emoji) for index in range(3)]
    feedback = repair_feedback('d' * 64, results)
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert [item['id'] for item in feedback['results']] == ['c00-check', 'c01-check', 'c02-check']
    assert all(item['output'] for item in feedback['results'])


def test_dropping_takes_from_the_owner_holding_the_most():
    wide = '\u00e9' * 2000
    results = [_result(f'c00-{index}', 'fail', wide) for index in range(14)] + [
        _result('c01-a', 'fail', wide), _result('c01-b', 'fail', wide)]
    kept = [item['id'] for item in repair_feedback('d' * 64, results)['results']]
    assert {'c01-a', 'c01-b'} <= set(kept)


def test_a_long_context_is_bounded_and_the_feedback_fits():
    long_context = {**_result('c00-x', 'fail'), 'context': 'k' * 17000}
    feedback = repair_feedback('d' * 64, [long_context])
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert long_context['context'] == 'k' * 17000


def test_the_byte_bound_always_holds_and_the_report_is_never_altered():
    import copy

    results = [{**_result(f'c{index:02d}-check', 'fail', '\U0001F600' * 4000), 'context': '\U0001F600' * 256,
                'manifest_digest': 'f' * 64, 'exit_code': 2**40} for index in range(16)]
    original = copy.deepcopy(results)
    feedback = repair_feedback('d' * 64, results)
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert results == original
    assert feedback['results'] and all('manifest_digest' not in item for item in feedback['results'])
    assert all(item['exit_code'] is None for item in feedback['results'])


def test_the_last_fallback_drops_whole_results_when_ids_alone_exceed_the_bound():
    """Sixteen single-result owners whose ids alone (bounded at 128 characters) overflow, with nothing left to shorten."""
    results = [_result(f'c{index:02d}-' + '\U0001F600' * 124, 'fail') for index in range(16)]
    feedback = repair_feedback('d' * 64, results)
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert 0 < len(feedback['results']) < 16
    assert [item['id'][:4] for item in feedback['results']] == [f'c{index:02d}-' for index in range(len(feedback['results']))]
