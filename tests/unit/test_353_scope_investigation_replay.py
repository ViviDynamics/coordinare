"""Offline replay must preserve old protocol failures without inventing requests."""
from __future__ import annotations

import json

import pytest


def test_replay_reports_repeated_old_source_reads(tmp_path):
    from scripts.replay_scope_investigation import replay

    replies = [
        {'tool': 'read_file', 'path': 'guide'},
        {'scope': {'criteria': []}, 'finding_responses': []},
        {'scope_assessment': {'verdict': 'revise', 'rationale': 'Inspect nested source', 'sources': ['guide'],
            'findings': [{'category': 'coverage', 'issue': 'Nested file is unread', 'sources': ['guide'], 'affected_ids': []}],
            'decisions': []}},
        {'scope_revision': {}, 'finding_responses': []},
        {'tool': 'read_file', 'path': 'guide'},
        {'scope_revision': {}, 'finding_responses': []},
    ]
    for index, reply in enumerate(replies, 1):
        (tmp_path/f'chat-{index:03}.json').write_text(json.dumps({'messages': [], 'response': json.dumps(reply)}))
    result = replay(tmp_path)
    assert result['replayed_calls'] == 6
    assert result['post_author_source_tools'] == 1 and result['new_selected_sources_after_author'] == []
    assert result['revisions_without_new_source_reads'] == 2
    assert result['structured_inspection_requests'] == []
    assert result['scope_reviews_missing_protocol_fields'] == 1
    assert result['corrected_protocol_rejection'] == 'inspection_requests_required'
    assert result['recorded_fetch_count'] is None
    assert result['executed_project_commands'] is False


def test_replay_keeps_explicit_requests_separate_from_read_citations(tmp_path):
    from scripts.replay_scope_investigation import replay

    reply = {'scope_assessment': {'verdict': 'revise', 'rationale': 'Inspect nested source', 'sources': ['guide'],
        'findings': [{'category': 'coverage', 'issue': 'Nested source needed', 'sources': ['guide'], 'affected_ids': [],
            'gap_kind': 'unread_source', 'inspection_requests': [{'operation': 'read_file', 'path': 'unfamiliar/nested'}]}], 'decisions': []}}
    (tmp_path/'chat-001.json').write_text(json.dumps({'response': json.dumps(reply)}))
    result = replay(tmp_path)
    assert result['structured_inspection_requests'] == [{'operation': 'read_file', 'path': 'unfamiliar/nested'}]
    assert result['new_selected_sources_after_author'] == []
    assert result['scope_reviews_missing_protocol_fields'] == 0
    assert result['source_tool_calls'] == 0


@pytest.mark.asyncio
async def test_completed_source_investigation_cannot_waive_conflict():
    from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
    from tests.unit.test_353_scope_contract import criterion, proposal, review, revision
    from tests.unit.test_353_scope_investigation_flow import discover, objection

    budget = AttemptBudget()
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])]),
        revision(criterion('nested'), sources=['nested']),
        review('indeterminate', sources=['guide', 'nested'], decisions=[{'finding_id': 'F0001', 'status': 'open',
            'rationale': 'The source conflict remains unresolved', 'sources': ['nested'], 'duplicate_of': None}])]
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discover(values, {'guide': 'See nested', 'nested': 'Conflicting source obligation'}, budget=budget)
    assert held.value.code == 'scope_review_indeterminate'
    assert len(budget.scope_investigation_results) == 1
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_contract is None


def test_source_acceptance_cannot_waive_failed_or_unrun_clean_check(tmp_path):
    from coordinare.services.environment_acceptance import (
        accepted_environment,
        saved_execution_accepted,
    )
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    assert accepted_environment(cache) is not None and saved_execution_accepted(cache)
    for status, code in [('fail', 2), ('not_run', None)]:
        cache.environment_verification['results'][0].update(status=status, exit_code=code)
        assert accepted_environment(cache) is not None
        assert not saved_execution_accepted(cache)


def test_replay_rejects_ambiguous_transcript_fields(tmp_path):
    from scripts.replay_scope_investigation import replay

    (tmp_path/'chat-001.json').write_text('{"response":"{}","response":"{}"}')
    with pytest.raises(ValueError, match='duplicate'):
        replay(tmp_path)
