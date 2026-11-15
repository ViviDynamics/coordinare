from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from performer.models import Score, Stand
from performer.security_floor import run_security_floor
from performer.security_scanner import scan_diff

HOSTILE = 'secret-token /private/workspace/source.py raw scanner output'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,expected', [
    ('request', 'unsupported or missing security scan request'),
    ('head', 'security scan head changed or unavailable'),
    ('git', 'git rev-parse failed'),
    ('timeout', 'semgrep timed out'),
    ('malformed', 'semgrep emitted unparseable JSON'),
    ('exit', 'semgrep failed'),
    ('unexpected', 'unexpected security scan failure'),
])
async def test_safe_failure_diagnostics(tmp_path, failure, expected):
    request = {'version': 1, 'diff_status': 'ok', 'files': ['app.py'], 'head_sha': 'head'}
    score = Score(title='Card', repo_url='https://github.com/example/project', branch='fix',
                  security_floor=None if failure == 'request' else request)

    def run(*args, **kwargs):
        if failure == 'git':
            raise subprocess.CalledProcessError(128, HOSTILE, output=HOSTILE, stderr=HOSTILE)
        if 'ls-tree' in args[0]:
            return SimpleNamespace(stdout=b'')
        return SimpleNamespace(stdout=b'other' if failure == 'head' else b'head')

    def scan(*args):
        if failure == 'unexpected':
            raise type(HOSTILE, (Exception,), {})(HOSTILE)
        effect = subprocess.TimeoutExpired(HOSTILE, 120, output=HOSTILE) if failure == 'timeout' else None
        stdout = HOSTILE if failure == 'malformed' else '{"results": []}'
        with patch('performer.security_scanner._resolve_binary', return_value='/usr/bin/semgrep'), patch(
            'performer.security_scanner.subprocess.run', side_effect=effect,
            return_value=SimpleNamespace(stdout=stdout, returncode=2),
        ):
            return scan_diff(['app.py'], tmp_path)

    with capture_logs() as logs, patch('performer.security_floor.subprocess.run', side_effect=run), patch(
        'performer.security_floor._head_targets', return_value=['app.py'],
    ), patch('performer.security_floor._scan_export', side_effect=scan):
        report = await run_security_floor(score, Stand(path=tmp_path, branch='fix'))
    assert report['complete'] is False
    assert report['reason'] == expected
    assert HOSTILE not in str(report) + str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize('error_kind', ['generic', 'diagnostic', 'setup'])
async def test_exception_text_never_crosses_report_or_status(tmp_path, error_kind):
    from performer.backends.base import BackendStatus
    from performer.security_scanner import ScanDiagnostic, ScannerError, ScannerSetupError
    from performer.status_paths import security_path

    error = {'generic': ScannerError, 'diagnostic': ScanDiagnostic, 'setup': ScannerSetupError}[error_kind](HOSTILE)
    score = Score(title='Card', repo_url='https://github.com/example/project', branch='fix')
    with capture_logs() as logs, patch('performer.security_floor._scan_head', side_effect=error):
        report = await run_security_floor(score, Stand(path=tmp_path, branch='fix'))
    perf = SimpleNamespace(score=score, security_floor_report=report, session_id='session')
    response = await security_path(perf, BackendStatus(state='done', output=''), None)
    assert response.status == 'env_blocked'
    assert report['reason'] in response.reason
    assert HOSTILE not in str(report) + str(logs) + response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['semgrep', 'bandit'])
@pytest.mark.parametrize('stdout,code,expected', [
    ('', 2, 'produced no output'),
    ('{}', 0, 'emitted malformed results'),
    ('{"results": [], "errors": ["secret-token"]}', 0, 'reported scan errors'),
    ('{"results": [{"extra": "secret-token", "line_number": "secret-token"}]}', 0,
     'emitted malformed results'),
    ('{"results": [{"start": {"line": 1e999}, "line_number": 1e999}]}', 0,
     'emitted malformed results'),
    ('{"results": [{"start": {"line": -1e999}, "line_number": -1e999}]}', 0,
     'emitted malformed results'),
])
async def test_scanner_identity_and_payload_errors_are_safe(tmp_path, tool, stdout, code, expected):
    def run(cmd, **kwargs):
        return SimpleNamespace(stdout=stdout if cmd[0].endswith(tool) else '{"results": []}',
                               returncode=code if cmd[0].endswith(tool) else 0)

    score = Score(title='Card', repo_url='https://github.com/example/project', branch='fix')
    with capture_logs() as logs, patch('performer.security_floor._scan_head',
                                      side_effect=lambda *args: (scan_diff(['app.py'], tmp_path), True)), patch(
        'performer.security_scanner._resolve_binary', side_effect=lambda name: '/usr/bin/' + name,
    ), patch('performer.security_scanner.subprocess.run', side_effect=run):
        report = await run_security_floor(score, Stand(path=tmp_path, branch='fix'))
    assert report['complete'] is False
    assert report['reason'] == f'{tool} {expected}'
    assert 'secret-token' not in str(report) + str(logs)
