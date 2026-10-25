"""A private probe guest that cannot start or runs out of time is that command's result, not the attempt's end;
an unconfirmed teardown still fails the probe (SampleApp v9 and v15 held at probe_executor_failed)."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment import observation
from coordinare_service_inference.environment.runtime_observation import PrivateTeardownError

DATA = {'runtime': {}, 'commands': [{'id': 'node-versions'}], 'seconds': 30}


def failing(exc: BaseException):
    def run(operation):
        operation.close()
        raise exc
    return run


@pytest.mark.parametrize(('exc', 'status'), [(RuntimeError('guest boot failed at /private/path'), 'execution_error'),
                                             (OSError('no space'), 'execution_error'),
                                             (TimeoutError('deadline'), 'timed_out')])
def test_a_guest_failure_is_reported_for_its_command_only(monkeypatch, exc, status):
    monkeypatch.setattr(observation, 'observe_private', lambda data: _coroutine())
    monkeypatch.setattr(observation, 'run_cli', failing(exc))
    [result] = observation._private_results(dict(DATA))
    assert result['id'] == 'node-versions' and result['status'] == status and result['exit_code'] is None
    assert '/private/path' not in result['output']


def test_an_unconfirmed_teardown_still_fails_the_probe(monkeypatch):
    monkeypatch.setattr(observation, 'observe_private', lambda data: _coroutine())
    monkeypatch.setattr(observation, 'run_cli', failing(PrivateTeardownError('teardown is incomplete')))
    with pytest.raises(PrivateTeardownError):
        observation._private_results(dict(DATA))


async def _noop() -> list:
    return []


def _coroutine():
    return _noop()
