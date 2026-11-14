"""The failure a repair's authors need reaches them: the runner's leading excerpt, the cause, the earlier attempts.

Dettmore e2e-54: the repair record kept the last 512 characters of each output, cutting the build-log excerpt the
runner puts first, so no author saw "OpenSSL library directory could not be found"; consequences read as faults of
their own; and each repair author saw only the latest execution, so repairs fixed failures earlier repairs caused.
"""
from __future__ import annotations

import json

from coordinare.services.environment_repair import (
    FEEDBACK_MAX_BYTES,
    HISTORY_MAX_BYTES,
    first_error_line,
    keep_ends,
    repair_attempt_history,
    repair_feedback,
)

EXCERPT = ('[tail of the build log this output names]\nopenssl:\n\tOpenSSL library directory could not be found\n'
           '[... output]\n')


def _result(identifier, status, output='', required=True, exit_code=1):
    return {'id': identifier, 'status': status, 'required': required, 'output': output, 'exit_code': exit_code}


def test_keep_ends_keeps_the_leading_excerpt_and_the_last_lines():
    text = EXCERPT + 'compiling\n' * 3000 + 'external command failed with status 2\n'
    kept, cut = keep_ends(text, 2048)
    assert cut and len(kept) <= 2048
    assert 'OpenSSL library directory could not be found' in kept
    assert kept.endswith('external command failed with status 2\n')
    assert 'characters omitted' in kept


def test_keep_ends_leaves_short_text_alone():
    assert keep_ends('fine\n', 100) == ('fine\n', False)


def test_keep_ends_holds_its_bound_even_when_tiny():
    kept, cut = keep_ends('x' * 500, 10)
    assert cut and len(kept) == 10


def test_feedback_halving_keeps_the_excerpt_at_the_start():
    big = EXCERPT + 'compiling\n' * 4000 + 'BUILD FAILED\n'
    feedback = repair_feedback('d' * 64, [_result('install-lang', 'fail', big)], install_ids={'install-lang'})
    output = feedback['results'][0]['output']
    assert len(json.dumps(feedback).encode()) <= FEEDBACK_MAX_BYTES
    assert 'OpenSSL library directory could not be found' in output and output.endswith('BUILD FAILED\n')


def test_a_failure_after_a_failed_install_names_it_as_blocking_and_as_probable_cause():
    results = [_result('install-lang', 'fail', 'BUILD FAILED'), _result('install-chart-tool', 'fail', 'curl: (22) 404'),
               _result('start-mock', 'fail', "env: 'lang': No such file or directory", exit_code=127),
               _result('check-unrelated', 'fail', 'assertion failed')]
    feedback = repair_feedback('d' * 64, results, install_ids={'install-lang', 'install-chart-tool'})
    by_id = {item['id']: item for item in feedback['results']}
    assert by_id['start-mock']['blocked_by'] == ['install-lang', 'install-chart-tool']
    assert by_id['start-mock']['probable_cause'].startswith('install-lang failed earlier')
    assert by_id['check-unrelated']['blocked_by'] == ['install-lang', 'install-chart-tool']
    assert 'probable_cause' not in by_id['check-unrelated']
    # An install is never "blocked" by an earlier one; its own failure stays its own.
    assert 'blocked_by' not in by_id['install-chart-tool'] and 'blocked_by' not in by_id['install-lang']


def test_no_install_ids_means_no_cause_hints():
    results = [_result('install-lang', 'fail', 'x'), _result('start-mock', 'fail', 'lang missing')]
    assert all('blocked_by' not in item for item in repair_feedback('d' * 64, results)['results'])


def _entry(digest, install_argv, results):
    return {'manifest_digest': digest,
            'manifest': {'install': [{'id': 'install-lang', 'argv': install_argv}, {'id': 'install-db', 'argv': ['db']}],
                         'checks': [{'id': 'check-suite', 'argv': ['test']}], 'services': []},
            'report': {'results': results}}


def test_history_summarises_each_attempt_with_what_changed_and_its_first_error():
    rejected = [
        _entry('a' * 64, ['build', '--with-dir=/wrong'],
               [_result('install-lang', 'fail', EXCERPT + 'BUILD FAILED'), _result('check-suite', 'not_run')]),
        _entry('b' * 64, ['fetch', 'https://example.invalid/lang.tar'],
               [_result('install-lang', 'fail', 'curl: (22) The requested URL returned error: 404')]),
        _entry('c' * 64, ['fetch', 'https://example.invalid/3/lang.tar'], [_result('install-db', 'fail', 'boom')]),
    ]
    history = repair_attempt_history(rejected)
    assert [item['attempt'] for item in history] == [1, 2, 3]
    assert history[0]['summary'].startswith('attempt 1: first execution; 1 required failed; first error install-lang: ')
    assert 'OpenSSL library directory could not be found' in history[0]['summary']
    assert history[1]['summary'].startswith('attempt 2: changed install-lang;')
    assert '404' in history[1]['summary']
    assert history[1]['changed_commands'] == {'install-lang': {'id': 'install-lang',
                                                               'argv': ['fetch', 'https://example.invalid/lang.tar']}}
    # Earlier attempts carry their failure output; the latest is already the execution feedback.
    assert history[0]['failure_output'][0]['id'] == 'install-lang'
    assert 'failure_output' not in history[2]


def test_history_stays_within_its_bound_and_keeps_every_summary():
    noisy = 'é' * 6000
    rejected = [_entry(str(index) * 64, [f'build-{index}'],
                       [_result(f'c{n:02d}-check-{index}', 'fail', noisy) for n in range(12)]
                       + [_result('install-lang', 'fail', noisy)])
                for index in range(1, 9)]
    history = repair_attempt_history(rejected)
    assert len(json.dumps(history).encode()) <= HISTORY_MAX_BYTES
    assert len(history) == 8 and all(item['summary'] for item in history)


def test_first_error_line_prefers_a_stated_error_over_noise():
    assert first_error_line('checking...\nok\nfatal: repository not found\nlast') == 'fatal: repository not found'
    assert first_error_line('just output\nlast line\n') == 'last line'
    assert first_error_line('') == ''
