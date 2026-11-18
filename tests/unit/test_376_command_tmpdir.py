"""nare-native-015: every sampled pytest check crashed truncating its capture file on the host-bound carrier /tmp.

Manifest and probe commands get one container-local TMPDIR; the private runtime keeps its own /tmp layout.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_commands_get_the_container_local_tmpdir_only_where_the_carrier_provides_it(tmp_path: Any,
                                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare_service_inference.environment import runner

    env = {'TMPDIR': '/inherited'}
    monkeypatch.setattr(runner, 'COMMAND_TMPDIR', str(tmp_path / 'absent'))
    runner.command_tmpdir(env)
    assert env['TMPDIR'] == '/inherited'
    monkeypatch.setattr(runner, 'COMMAND_TMPDIR', str(tmp_path))
    runner.command_tmpdir(env)
    assert env['TMPDIR'] == str(tmp_path)


def test_a_manifest_command_gets_it_and_its_own_activation_still_wins(tmp_path: Any,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare_service_inference.environment import runner

    from tests.unit.test_351_environment_runner import contract

    monkeypatch.setattr(runner, 'COMMAND_TMPDIR', str(tmp_path))
    roots = {'cache': '/cache', 'repo': '/repo', 'scratch': '/scratch'}
    plain = contract([{'id': 'base', 'argv': ['true']}])
    assert runner.manifest_environment(plain, **roots)['TMPDIR'] == str(tmp_path)
    own = contract([{'id': 'base', 'argv': ['true']}], activation={'env': {'TMPDIR': '{scratch}/own'}, 'path': []})
    assert runner.manifest_environment(own, **roots)['TMPDIR'] == '/scratch/own'


def test_a_probe_gets_the_same_tmpdir_on_its_own_tmpfs(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare_service_inference.environment import observation, runner

    from coordinare.services.environment_probe_executor import probe_container_argv
    from tests.unit.test_353_environment_probes import plan

    argv = probe_container_argv(plan(), tmp_path, tmp_path)
    assert f'{runner.COMMAND_TMPDIR}:rw,nosuid,nodev,size=64m,mode=1777' in argv
    monkeypatch.setattr(runner, 'COMMAND_TMPDIR', str(tmp_path))
    env = observation.command_environment({'id': 'p', 'argv': ['true'], 'env': {}})
    assert env['TMPDIR'] == str(tmp_path)


def test_every_carrier_image_creates_it() -> None:
    from coordinare_service_inference.environment.runner import COMMAND_TMPDIR

    from coordinare.services.environment_carrier import CARRIER_IMAGE_USER_STEP

    assert CARRIER_IMAGE_USER_STEP.endswith(f'install -d -m 1777 {COMMAND_TMPDIR}')
    assert 'RUN ' + CARRIER_IMAGE_USER_STEP in (ROOT / 'agent/performer/Dockerfile.base').read_text()
    assert not COMMAND_TMPDIR.startswith('/tmp')
