"""nare-native-044 ran every manifest command as bash -lc. A login shell re-reads /etc/profile, which resets PATH, so
docker lost the runtime's ssh helper ("Host key verification failed") and checks would have lost activation.path. Its
repair then wrote "Host * / StrictHostKeyChecking no" into HOME's ssh config, which the operator declined."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare_service_inference.environment import runner
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services import environment_discovery as discovery
from coordinare.services.environment_carrier import (
    _HOOK_LINE,
    CARRIER_IMAGE_USER_STEP,
    LOGIN_PATH_HOOK,
)

ROOT = Path(__file__).resolve().parents[2]


def _manifest(paths: list[str]) -> EnvironmentManifest:
    return EnvironmentManifest.model_construct(activation=type('A', (), {'path': paths, 'env': {}})())


def test_the_daemons_path_prefix_is_named_for_login_shells(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv(runner.LOGIN_PATH_ENV, '/stale')
    roots = {'cache': str(tmp_path / 'c'), 'repo': str(tmp_path / 'r'), 'scratch': str(tmp_path / 's')}
    env = runner.manifest_environment(_manifest(['{cache}/venv/bin', '{cache}/tools/bin']), **roots,
                                      runtime_env={'PATH': '/work/bin:/usr/bin', 'DOCKER_HOST': 'ssh://root@127.0.0.1:2222'})
    leading = ['/work/bin', f'{roots["cache"]}/venv/bin', f'{roots["cache"]}/tools/bin']
    assert env[runner.LOGIN_PATH_ENV] == ':'.join(leading)
    assert env['PATH'].split(':')[:3] == leading
    # Nothing in front, nothing named: an inherited value never leaks into a command.
    plain = runner.manifest_environment(_manifest([]), **roots)
    assert runner.LOGIN_PATH_ENV not in plain


def test_the_profile_hook_puts_the_prefix_back_after_a_reset(tmp_path):
    hook = tmp_path / 'hook.sh'
    hook.write_text(_HOOK_LINE + '\n')
    script = f'PATH=/usr/bin:/bin; . {hook}; printf %s "$PATH"'
    restored = subprocess.run(['/bin/sh', '-c', script], env={runner.LOGIN_PATH_ENV: '/work/bin:/cache/venv/bin'},
                              capture_output=True, text=True, check=True).stdout
    assert restored == '/work/bin:/cache/venv/bin:/usr/bin:/bin'
    unset = subprocess.run(['/bin/sh', '-c', script], env={}, capture_output=True, text=True, check=True)
    assert unset.stdout == '/usr/bin:/bin' and unset.returncode == 0


def test_every_carrier_image_installs_the_hook():
    assert CARRIER_IMAGE_USER_STEP.endswith(f'> {LOGIN_PATH_HOOK}') and _HOOK_LINE in CARRIER_IMAGE_USER_STEP
    assert 'RUN ' + CARRIER_IMAGE_USER_STEP in (ROOT / 'agent/performer/Dockerfile.base').read_text()


def test_prompts_rule_out_ssh_weakening_for_authors_and_reviewers():
    # The login PATH hook above makes a login shell safe, so the prompts no longer forbid one; the runtime rule, which
    # every author and reviewer packet carries, still rules out writing ssh configuration and asks reviewers to reject it.
    assert 'Never write ssh configuration or keys' in discovery.RUNTIME_DOCKER
    assert 'StrictHostKeyChecking no' in discovery.RUNTIME_DOCKER and 'a reviewer rejects' in discovery.RUNTIME_DOCKER
    assert 'login shell' not in discovery.EXECUTION_RULES
    source = Path(discovery.__file__).read_text()
    assert '"runtime_docker": RUNTIME_DOCKER' in source and '"execution_rules": EXECUTION_RULES' in source
