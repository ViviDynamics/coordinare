"""The review prompt's execution history carries what a reviewer compares, never the rejected manifest's scope.

Dettmore e2e-08: the history copied the whole rejected manifest, a 387 KB scope contract and 52 KB of input digests,
into every review shard's shared prompt (638 KB), past the model's context; the repaired candidate's review failed.
"""
from __future__ import annotations

import json

from coordinare.services.environment_repair import execution_review_evidence


def test_the_history_keeps_records_and_results_but_not_the_scope_or_input_digests():
    manifest = {'checks': [{'id': 'c00-a', 'argv': ['true']}], 'install': [], 'criterion_checks': {'S1': ['c00-a']},
                'scope_contract': {'definition': {'criteria': ['x' * 400_000]}}, 'scope_contract_digest': 'd' * 64,
                'inputs': {f'file-{index}': 'e' * 64 for index in range(500)}}
    report = {'results': [{'id': 'c00-a', 'status': 'fail', 'required': True, 'exit_code': 1, 'duration_seconds': 2.0,
                           'output': 'secret-ish output'}]}
    [entry] = execution_review_evidence([{'manifest_digest': 'm' * 64, 'manifest': manifest, 'report': report}])
    assert set(entry['manifest']) == {'checks', 'install', 'criterion_checks', 'scope_contract_digest'}
    assert entry['results'] == [{'id': 'c00-a', 'status': 'fail', 'required': True, 'exit_code': 1, 'duration_seconds': 2.0}]
    assert entry['failure_output'] == [{'id': 'c00-a', 'output': 'secret-ish output', 'output_truncated': False}]
    assert len(json.dumps(entry).encode()) < 2000
    assert 'scope_contract' in manifest


def test_failed_commands_carry_their_bounded_output_for_the_reviewer():
    """Dettmore e2e-46/47: shown statuses only, reviewers could not tell whether a repair answered install-ruby's failure."""
    manifest = {'checks': [], 'install': [{'id': 'install-ruby', 'argv': ['build-ruby']}]}
    report = {'results': [
        {'id': 'install-ruby', 'status': 'fail', 'required': True, 'exit_code': 1, 'duration_seconds': 238.0,
         'output': 'BUILD FAILED ' + 'x' * 100_000},
        {'id': 'install-npm', 'status': 'pass', 'required': True, 'exit_code': 0, 'duration_seconds': 3.0, 'output': 'ok'},
        {'id': 'c00-a', 'status': 'not_run', 'required': True, 'exit_code': None, 'duration_seconds': 0.0, 'output': ''}]}
    [entry] = execution_review_evidence([{'manifest_digest': 'm' * 64, 'manifest': manifest, 'report': report}])
    [failure] = entry['failure_output']
    assert failure['id'] == 'install-ruby' and failure['output'].startswith('BUILD FAILED') and failure['output_truncated']
    assert len(json.dumps(entry).encode()) < 20 * 1024
