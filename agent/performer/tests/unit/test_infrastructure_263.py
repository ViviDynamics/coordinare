"""263: infrastructure errors stop before any model repair or local retry."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import base64
import pytest
import respx

from performer.github import get_check_runs
from performer.infrastructure import InfrastructureBlocked
from performer.test_results import _match_env_signature
from performer.workflows.implementer.baseline import run_tests
from performer.workflows.implementer.ci import run_ci_phase
from performer.workflows.implementer.quality import run_quality_phase

REGISTRY = 'Error response from daemon: login attempt to https://docker.***.com/v2/ failed with status: 403 Forbidden'


@pytest.mark.asyncio
@respx.mock
async def test_annotations_reach_workflow_hold_with_zero_repairs():
    root = 'https://api.github.com/repos/org/repo'
    respx.get(root + '/commits/abc/check-runs').respond(200, json={'check_runs': [{
        'id': 193, 'name': 'build', 'status': 'completed', 'conclusion': 'failure', 'output': {'title': '', 'summary': ''},
    }]})
    respx.get(root + '/check-runs/193/annotations').respond(200, json=[{'annotation_level': 'failure', 'message': REGISTRY}])
    checks = await get_check_runs('org', 'repo', 'abc', 'test')
    assert REGISTRY in checks[0]['output']['summary']
    ctx = SimpleNamespace(get_check_runs=AsyncMock(return_value=checks), github_api_calls=0,
                          budgets=SimpleNamespace(ci_wait_s=1, ci_repairs=3), push=AsyncMock(),
                          toolkit=SimpleNamespace(agent_turn=AsyncMock()))
    with pytest.raises(InfrastructureBlocked, match='Registry authentication'):
        await run_ci_phase(ctx, head_sha='abc', quality=[], rerun_green=AsyncMock())
    ctx.push.assert_not_awaited()
    ctx.toolkit.agent_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['tests', 'quality'])
async def test_local_failure_holds_before_repairs(tmp_path, phase):
    toolkit = SimpleNamespace(run_command=AsyncMock(return_value=SimpleNamespace(exit_code=1, output_excerpt=REGISTRY)),
                              agent_turn=AsyncMock())
    with pytest.raises(InfrastructureBlocked, match='Registry authentication'):
        if phase == 'tests':
            await run_tests(toolkit, 'pytest', 'pytest', tmp_path)
        else:
            ctx = SimpleNamespace(toolkit=toolkit, workspace=tmp_path, test_timeout_s=10,
                                  budgets=SimpleNamespace(quality_repairs=3))
            await run_quality_phase(ctx, ['make ci'], rerun_green=AsyncMock())
    toolkit.run_command.assert_awaited_once()
    toolkit.agent_turn.assert_not_awaited()


def test_real_failed_test_with_registry_fixture_is_still_a_code_failure():
    assert _match_env_signature(REGISTRY + '\n1 failed, 4 passed in 1.0s') is None
    assert 'Registry authentication' in _match_env_signature(REGISTRY)


@pytest.mark.asyncio
@respx.mock
async def test_job_setup_survives_annotations_permission_failure():
    root = 'https://api.github.com/repos/org/repo'
    respx.get(root + '/commits/abc/check-runs').respond(200, json={'check_runs': [{
        'id': 193, 'name': 'build', 'status': 'completed', 'conclusion': 'failure',
        'details_url': 'https://untrusted.test/actions/runs/11/job/22',
    }]})
    respx.get(root + '/check-runs/193/annotations').respond(403)
    respx.get(root + '/actions/jobs/22').respond(200, json={'name': 'build', 'steps': [{'name': 'Custom setup name', 'conclusion': 'failure'}]})
    respx.get(root + '/actions/runs/11').respond(200, json={'path': '.github/workflows/build.yml', 'head_sha': 'abc'})
    document = 'jobs:\n  build:\n    steps:\n      - name: Custom setup name\n        uses: ruby/setup-ruby@v1\n      - run: make test\n'
    respx.get(root + '/contents/.github/workflows/build.yml').respond(200, json={'content': base64.b64encode(document.encode()).decode()})
    checks = await get_check_runs('org', 'repo', 'abc', 'test')
    assert checks[0]['setup_failure'] is True
    assert all(call.request.url.host == 'api.github.com' for call in respx.calls)
