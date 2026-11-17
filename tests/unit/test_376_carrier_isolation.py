"""Carriers run every manifest command as an unprivileged user that can still create a user-namespace sandbox."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from coordinare_service_inference.environment import runtime_supervisor

from coordinare.services import environment_carrier
from coordinare.services.environment_carrier import (
    CARRIER_IMAGE_USER_STEP,
    CARRIER_UID,
    DOCKER_DEFAULT_PROFILE,
    USER_NAMESPACE_RULE,
    carrier_isolation_options,
    carrier_seccomp_path,
    carrier_seccomp_profile,
)

REPO = Path(__file__).resolve().parents[2]


def test_the_profile_is_docker_default_plus_only_the_user_namespace_rule() -> None:
    default = json.loads(DOCKER_DEFAULT_PROFILE.read_text())
    profile = carrier_seccomp_profile()
    assert {key: value for key, value in profile.items() if key != 'syscalls'} == {
        key: value for key, value in default.items() if key != 'syscalls'}
    assert profile['syscalls'] == [*default['syscalls'], USER_NAMESPACE_RULE]
    assert profile['defaultAction'] == 'SCMP_ACT_ERRNO'
    assert sorted(USER_NAMESPACE_RULE['names']) == ['clone', 'mount', 'pivot_root', 'umount2', 'unshare']
    # Nothing the default grants only with CAP_SYS_ADMIN is opened beyond the namespace and mount calls.
    assert not {'bpf', 'perf_event_open', 'setns', 'syslog', 'quotactl'} & set(USER_NAMESPACE_RULE['names'])


def test_a_changed_vendored_profile_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    changed = tmp_path / 'default.json'
    changed.write_text(DOCKER_DEFAULT_PROFILE.read_text().replace('SCMP_ACT_ERRNO', 'SCMP_ACT_ALLOW', 1))
    monkeypatch.setattr(environment_carrier, 'DOCKER_DEFAULT_PROFILE', changed)
    with pytest.raises(RuntimeError, match='digest'):
        carrier_seccomp_profile()


def test_options_run_unprivileged_with_the_profile_from_a_private_file() -> None:
    options = carrier_isolation_options()
    assert options[options.index('--user') + 1] == f'{CARRIER_UID}:{CARRIER_UID}'
    assert options[options.index('--cap-drop') + 1] == 'ALL'
    security = [options[index + 1] for index, option in enumerate(options) if option == '--security-opt']
    assert security == ['no-new-privileges', f'seccomp={carrier_seccomp_path()}', 'systempaths=unconfined']
    assert 'HOME=/tmp/home' in options
    path = Path(carrier_seccomp_path())
    assert json.loads(path.read_text()) == carrier_seccomp_profile()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_every_carrier_image_defines_the_unprivileged_account() -> None:
    assert f'useradd -u {CARRIER_UID}' in CARRIER_IMAGE_USER_STEP
    assert 'RUN ' + CARRIER_IMAGE_USER_STEP in (REPO / 'agent/performer/Dockerfile.base').read_text()
    source = (REPO / 'scripts/environment_validation_image.py').read_text()
    assert 'CARRIER_IMAGE_USER_STEP' in source


def test_the_runtime_works_beside_an_unwritable_scratch_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    assert runtime_supervisor.runtime_work_parent(scratch) == tmp_path
    monkeypatch.setattr(runtime_supervisor.os, 'access', lambda path, mode: False)
    assert runtime_supervisor.runtime_work_parent(scratch) == Path('/var/tmp')
