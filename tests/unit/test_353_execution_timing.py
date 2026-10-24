"""Per-command guest durations survive into the persisted report and into run events, with a phase total."""
from __future__ import annotations

import json

from coordinare_service_inference.environment.models import VerificationReport
from scripts.validate_environment_repositories import command_timing


def _report(**results):
    return {
        'manifest_digest': 'a' * 64, 'scope': '', 'platform': 'linux/x86_64', 'results': [
            {'id': 'install-gh', 'status': 'pass', 'required': True, 'exit_code': 0, 'duration_seconds': 12.25},
            {'id': 'install-helm', 'status': 'fail', 'required': True, 'exit_code': 1, 'duration_seconds': 0.5},
            {'id': 'verify-ruby', 'status': 'not_run', 'required': True, 'duration_seconds': 0},
        ]}


def test_phase_total_sums_only_commands_that_ran_and_emits_one_event_each(capsys):
    total = command_timing('installer', 'dettmore', _report())
    assert total == 12.75
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['command'] for event in events] == ['install-gh', 'install-helm']
    assert {event['stage'] for event in events} == {'installer_command'}
    assert events[0]['seconds'] == 12.25 and events[0]['status'] == 'pass'
    assert all('output' not in event for event in events)


def test_missing_or_unparsed_evidence_contributes_no_time(capsys):
    assert command_timing('clean_consumer', 'dettmore', None) == 0
    assert command_timing('clean_consumer', 'dettmore', {'results': []}) == 0
    assert capsys.readouterr().out == ''


def test_per_command_duration_is_kept_in_the_persisted_report():
    persisted = json.loads(VerificationReport.model_validate(_report()).model_dump_json())
    assert [result['duration_seconds'] for result in persisted['results']] == [12.25, 0.5, 0]
