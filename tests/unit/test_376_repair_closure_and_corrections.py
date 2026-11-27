"""nare-native-045 held on attempt_schema_limit (16/16) with 1,489 calls and 3.9 hours left.

A finding naming the guard requirements, not c-guards, left c-guards frozen; the delta revision's rewrite of it was put
back silently, review read it as unanswered, and the delta fell back to a full repair. That repair rewrote the manifest
from scratch and was told its losses one per correction, while reviewers' format slips drew on the same sixteen.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import (
    Command,
    EnvironmentManifest,
    Requirement,
)

from coordinare.services import environment_discovery as discovery
from coordinare.services.environment_attempt import (
    SCHEMA_BACKSTOP_FACTOR,
    AttemptBudget,
    EnvironmentDiscoveryError,
)
from coordinare.services.environment_delta import STANDING_EXCLUDED, DeltaPlan
from tests.unit.test_353_repair_context import _cmd, _part, _plan


def _requirement(identifier: str, *checks: str) -> Requirement:
    return Requirement.model_validate({'id': identifier, 'description': identifier, 'source': 'README.md',
                                       'checks': list(checks)})


def _manifest(**fields) -> EnvironmentManifest:
    values = {'requirements': [], 'criterion_checks': {}, 'install': [], 'checks': [], 'services': []} | fields
    return EnvironmentManifest.model_construct(**values)


def test_a_finding_naming_a_requirement_or_criterion_names_its_checks():
    manifest = _manifest(requirements=[_requirement('req-guards', 'c-guards', 'c-wiring')],
                         criterion_checks={'S7': ['c-bench']})
    assert discovery.finding_records(manifest, {'req-guards'}) == {'req-guards', 'c-guards', 'c-wiring'}
    assert discovery.finding_records(manifest, {'S7', 'c-ruff'}) == {'S7', 'c-bench', 'c-ruff'}
    assert discovery.finding_records(None, {'req-guards'}) == {'req-guards'}


def test_a_frozen_record_put_back_is_shown_to_review():
    plan = _plan({'install-tool'})
    assert discovery.restored_records(plan) == {}
    authored = _part(checks=[_cmd('check-tool', 'rewritten'), _cmd('check-other', 'c')])
    plan.restore_frozen(_part(), authored)
    assert discovery.restored_records(plan) == {'daemon_restored_records': ['check-tool']}
    assert discovery.restored_records(None) == {}
    assert discovery.RESTORED_RECORDS_REVIEW in discovery.EXECUTABLE_REVIEW_CRITERIA
    assert 'daemon_restored_records' in STANDING_EXCLUDED
    # A plan widened by replace keeps what was restored.
    from dataclasses import replace
    assert replace(plan, fixed=frozenset({'x'})).restored == {'check-tool'}
    assert isinstance(plan, DeltaPlan)


def test_install_ids_are_dropped_from_clean_dependencies():
    data = {'install': [_cmd('inst-venv'), _cmd('inst-gh')],
            'checks': [_cmd('c-ruff', depends_on=['inst-venv', 'c-import']), _cmd('c-import')],
            'services': [{'id': 'db', 'start': _cmd('db-start', depends_on=['inst-gh']), 'health': _cmd('db-health')}]}
    assert discovery.strip_install_dependencies(data) == ['c-ruff', 'db-start']
    assert data['checks'][0]['depends_on'] == ['c-import']
    assert data['services'][0]['start']['depends_on'] == []
    assert discovery.strip_install_dependencies(data) == []


def test_each_native_session_has_its_own_schema_allowance_within_a_backstop():
    budget = AttemptBudget(max_calls=99, max_reads=99, max_coverage_revisions=1, max_schema_repairs=4)
    for _ in range(4):
        budget.reserve_repair('schema', session='reviewer')
    with pytest.raises(EnvironmentDiscoveryError) as spent:
        budget.reserve_repair('schema', session='reviewer')
    assert spent.value.code == 'attempt_schema_limit'
    # Another session still has its own four, up to the generation backstop.
    assert budget.schema_repairs_left('author') == 4
    for _ in range(4):
        budget.reserve_repair('schema', session='author')
    assert budget.schema_repairs == SCHEMA_BACKSTOP_FACTOR * 4
    assert budget.schema_repairs_left('third') == 0
    assert budget.summary()['max_phase_schema_repairs'] == 4
    # Without a session (the JSON transport) the generation total is the limit, as before.
    legacy = AttemptBudget(max_calls=99, max_reads=99, max_coverage_revisions=1, max_schema_repairs=2)
    legacy.reserve_repair('schema')
    legacy.reserve_repair('schema')
    with pytest.raises(EnvironmentDiscoveryError):
        legacy.reserve_repair('schema')


def test_the_journal_keeps_each_sessions_corrections():
    from coordinare.services.environment_repair import RepairJournal
    fields = RepairJournal.model_fields
    assert 'agent_phase_schema_repairs' in fields
    assert fields['schema_repairs'].metadata[-1].le == SCHEMA_BACKSTOP_FACTOR * 16


def test_every_lost_obligation_is_named_in_one_refusal():
    ledger = discovery._ObligationLedger()
    ledger.install = {name: Command.model_validate(_cmd(name)) for name in ('inst-a', 'inst-b')}
    ledger.checks = {'c-guards': Command.model_validate(_cmd('c-guards'))}
    ledger.coverage = {'req-guards': {'c-guards'}, 'req-wiring': {'c-guards'}}
    data = {'install': [], 'checks': [_cmd('c-guards'), _cmd('c-other')],
            'requirements': [{'id': 'req-guards', 'description': 'g', 'source': 'README.md', 'checks': ['c-other']},
                             {'id': 'req-wiring', 'description': 'w', 'source': 'README.md', 'checks': ['c-other']}]}
    with pytest.raises(EnvironmentDiscoveryError) as refused:
        ledger.record(data)
    message = str(refused.value)
    assert refused.value.code == 'obligation_removed' and refused.value.global_owned
    assert 'inst-a, inst-b' in message and 'requirement req-guards' in message and 'requirement req-wiring' in message


def test_a_refusal_carries_the_replys_other_correctable_errors():
    exc = EnvironmentDiscoveryError('required obligation removed during repair: c-x', code='obligation_removed')
    exc.extra_feedback = [{'location': ['finding_responses'], 'code': 'finding_response_ids_invalid'}]
    budget = SimpleNamespace(retirable_checks=frozenset())
    feedback = discovery._declaration_feedback(exc, SimpleNamespace(inputs={}), budget)
    assert [error['code'] for error in feedback] == ['obligation_removed', 'finding_response_ids_invalid']
    assert 'obligation_removed' in discovery.CORRECTABLE_DECLARATION_CODES


def test_a_full_repair_is_told_what_to_keep():
    previous = _manifest(requirements=[_requirement('req-guards', 'c-guards')], criterion_checks={'S7': ['c-guards']},
                         install=[Command.model_validate(_cmd('inst-gh'))],
                         checks=[Command.model_validate(_cmd('c-guards'))])
    budget = AttemptBudget(max_calls=9, max_reads=9, max_coverage_revisions=1, max_schema_repairs=1)
    budget.execution_attempt_history = [{'failed_ids': ['inst-gh']}]
    kept = discovery.repair_obligations(previous, budget)
    assert kept['required_ids']['install'] == ['inst-gh'] and kept['required_ids']['checks'] == ['c-guards']
    assert kept['required_coverage'] == {'req-guards': ['c-guards']}
    assert kept['criterion_checks'] == {'S7': ['c-guards']}
    assert kept['install_failures_only'] == ['inst-gh']
    budget.execution_attempt_history = [{'failed_ids': ['c-guards']}]
    assert 'install_failures_only' not in discovery.repair_obligations(previous, budget)
