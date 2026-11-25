"""nare-native-031: a docker-marked test sample left a container behind; teardown found it only at the end and blamed
every command whose argv mentioned docker, failing seven that exited 0. In a private runtime guest, Docker state is now
listed after each install and check command: whatever a command leaves fails that command, and is removed."""
from __future__ import annotations

from typing import Any

import pytest
from coordinare_service_inference.environment import runner
from coordinare_service_inference.environment.models import CheckResult, Command

GUEST = {'DOCKER_HOST': 'ssh://root@127.0.0.1:2222', 'PATH': '/usr/bin'}


async def _run(monkeypatch: pytest.MonkeyPatch, states: list[frozenset[tuple[str, str]] | None], *,
               residual: bool) -> tuple[dict[str, CheckResult], list[Any]]:
    async def execute(command: Command, **_: Any) -> CheckResult:
        return CheckResult(id=command.id, status='pass', required=True, output='ok', manifest_digest='d' * 64)

    listed = list(states)

    async def state(_env: dict[str, str]) -> frozenset[tuple[str, str]] | None:
        # The last listing repeats: a settle re-lists until its grace ends.
        return listed.pop(0) if len(listed) > 1 else listed[0]

    removed: list[Any] = []

    async def remove(left: Any, _env: dict[str, str]) -> None:
        removed.append(sorted(left))

    monkeypatch.setattr(runner, 'RESIDUE_SETTLE_SECONDS', 0.05)
    monkeypatch.setattr(runner, 'RESIDUE_POLL_SECONDS', 0.0)
    monkeypatch.setattr(runner, '_execute', execute)
    monkeypatch.setattr(runner, '_guest_docker_state', state)
    monkeypatch.setattr(runner, '_remove_guest_docker_state', remove)
    results: dict[str, CheckResult] = {}
    commands = [Command(id='check-image', argv=['docker', 'pull', 'alpine']),
                Command(id='check-sample', argv=['runner', '-m', 'docker'], depends_on=['check-image'])]
    await runner._commands(commands, roots={}, env=dict(GUEST), digest='d' * 64, scope='', results=results,
                           residual=residual)
    return results, removed


@pytest.mark.asyncio
async def test_the_command_that_left_docker_state_fails_and_its_state_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    empty: frozenset[tuple[str, str]] = frozenset()
    left = frozenset({('containers', 'nare-bench-282116e347ee')})
    results, removed = await _run(monkeypatch, [empty, empty, empty, left], residual=True)
    assert results['check-image'].status == 'pass'
    sample = results['check-sample']
    assert sample.status == 'fail' and 'nare-bench-282116e347ee' in sample.output
    assert runner.COMMAND_RESIDUAL_GUIDANCE in sample.output
    assert removed == [[('containers', 'nare-bench-282116e347ee')]]


@pytest.mark.asyncio
async def test_nothing_is_judged_when_the_state_cannot_be_listed_or_outside_a_runtime(
        monkeypatch: pytest.MonkeyPatch) -> None:
    results, removed = await _run(monkeypatch, [None, None, None, None], residual=True)
    assert {result.status for result in results.values()} == {'pass'} and removed == []
    results, removed = await _run(monkeypatch, [], residual=False)
    assert {result.status for result in results.values()} == {'pass'} and removed == []


@pytest.mark.asyncio
async def test_only_a_runtime_guest_daemon_is_ever_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []

    async def run(argv: Any, env: Any) -> bytes:
        called.append(argv)
        return b''

    monkeypatch.setattr(runner, '_docker_run', run)
    monkeypatch.setattr(runner.shutil, 'which', lambda *_args, **_kwargs: '/usr/bin/docker')
    for env in ({}, {'DOCKER_HOST': 'unix:///var/run/docker.sock'}, {'DOCKER_HOST': 'tcp://127.0.0.1:2375'}):
        assert await runner._guest_docker_state(env) is None
    assert called == []
    assert await runner._guest_docker_state(dict(GUEST)) == frozenset()
    assert len(called) == 3


@pytest.mark.asyncio
async def test_a_container_still_being_removed_is_not_blamed_once_it_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    """nare-native-039: a sample's `docker run --rm` container, killed at its timeout, was listed mid-removal as the
    command exited, failing a check whose test passed (the same check had passed a generation earlier)."""
    empty: frozenset[tuple[str, str]] = frozenset()
    removing = frozenset({('containers', 'nare-bench-616424c1c331')})
    results, removed = await _run(monkeypatch, [empty, empty, empty, removing, removing, empty], residual=True)
    assert {result.status for result in results.values()} == {'pass'} and removed == []


@pytest.mark.asyncio
async def test_state_created_while_settling_is_not_blamed_on_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    empty: frozenset[tuple[str, str]] = frozenset()
    kept = frozenset({('containers', 'left-by-sample')})
    later = frozenset({('containers', 'left-by-sample'), ('volumes', 'unrelated')})
    results, removed = await _run(monkeypatch, [empty, empty, empty, kept, later], residual=True)
    sample = results['check-sample']
    assert sample.status == 'fail' and 'left-by-sample' in sample.output and 'unrelated' not in sample.output
    assert removed == [[('containers', 'left-by-sample')]]
