"""263: Actions annotations and workflow steps reach the infrastructure classifier."""
from __future__ import annotations

import base64

import httpx
import pytest

from coordinare.services.ci_evidence import failed_step_context, fetch_failure_evidence
from coordinare.services.env_signature import match_env_signature
from coordinare.services.failure_signature import normalize_reason

RECORDED_193 = 'Error response from daemon: login attempt to https://docker.***.com/v2/ failed with status: 403 Forbidden'


@pytest.mark.asyncio
async def test_recorded_annotation_only_failure_reaches_registry_classifier(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith('/annotations'):
            return httpx.Response(200, json=[{'annotation_level': 'failure', 'message': RECORDED_193},
                                           {'annotation_level': 'notice', 'message': 'not a failure'}])
        if path.endswith('/jobs/22'):
            return httpx.Response(200, json={'name': 'image', 'steps': [{'name': 'Registry sign in', 'conclusion': 'failure'}]})
        if path.endswith('/runs/11'):
            return httpx.Response(200, json={'path': '.github/workflows/build.yml', 'head_sha': 'abc'})
        workflow = 'jobs:\n  image:\n    steps:\n      - name: Registry sign in\n        uses: docker/login-action@v3\n      - run: make test\n'
        return httpx.Response(200, json={'content': base64.b64encode(workflow.encode()).decode()})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    class GitHub:
        async def current_token(self):
            return 'test-token'
        def _rest_api_base(self):
            return 'https://api.github.test'

    evidence = await fetch_failure_evidence(GitHub(), 'org', 'repo', 33, 'https://github.test/org/repo/actions/runs/11/job/22')
    assert evidence['annotations'] == [RECORDED_193]
    assert evidence['failed_steps'] == ['Registry sign in']
    assert evidence['setup_failure'] is True
    reason = normalize_reason(None, '\n'.join(evidence['annotations'] + evidence['failed_steps']))
    assert match_env_signature(reason, []).pattern_id == 'registry_auth'
    assert all(request.url.host == 'api.github.test' for request in requests)


@pytest.mark.parametrize('steps,expected', [
    ([{'name': 'Prepare', 'uses': 'vendor/new-setup@v1'}, {'run': 'make test'}], True),
    ([{'run': 'make test'}, {'name': 'Prepare', 'uses': 'vendor/check-code@v1'}], False),
    ([{'run': 'make test'}, {'name': 'Prepare', 'uses': 'actions/cache@v4'}], True),
    ([{'name': 'Prepare', 'run': 'make test'}], False),
])
def test_structural_setup_detection_does_not_need_incident_regex(steps, expected):
    job = {'name': 'test', 'steps': [{'name': 'Prepare', 'conclusion': 'failure'}]}
    assert failed_step_context(job, {'jobs': {'test': {'steps': steps}}}) == (['Prepare'], expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('title', [None, 'The job failed'])
async def test_annotation_setup_failure_holds_two_cards_without_bounce_and_notifies_once(monkeypatch, title):
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer_ci_gate import (
        _ci_gate_state,
        _GitHubWithRollup,
        _rollup_payload,
    )

    async def evidence(*args):
        return {'annotations': [RECORDED_193], 'failed_steps': ['Registry sign in'], 'setup_failure': True}
    monkeypatch.setattr('coordinare.services.ci_evidence.fetch_failure_evidence', evidence)
    github = _GitHubWithRollup(_rollup_payload(contexts=[{
        '__typename': 'CheckRun', 'name': 'Bake arm64 image', 'databaseId': 193,
        'status': 'COMPLETED', 'conclusion': 'FAILURE', 'title': title, 'summary': '',
    }]))
    class Notifications:
        card_blocked_reminder_cooldown_seconds = 3600
        def __init__(self):
            self.events = []
        async def dispatch(self, event):
            self.events.append(event)
    notifications = Notifications()
    for card_id in ['first', 'second']:
        state = _ci_gate_state(github)
        state['current_card']['id'] = card_id
        state['symphony_configs']['default'].persona_scope.env_blocked_gate.enabled = True
        state['notification_service'] = notifications
        result = await monitor_performer(state)
        assert result['phase'] == 'monitoring_performer'
        assert result['agent_dispatch'] == {}
        assert result['bounce_counter'] == {}
        assert result['latest_ci_gate_decision']['verdict'] == 'hold'
        assert result['env_blocked']['cause']
    assert len([event for event in notifications.events if event.event_type.value == 'env_blocked']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('annotations_ok', [False, True])
async def test_partial_evidence_survives_independent_api_failures(monkeypatch, annotations_ok):
    def handler(request):
        if request.url.path.endswith('/annotations'):
            return httpx.Response(200, json=[{'annotation_level': 'failure', 'message': RECORDED_193}]) if annotations_ok else httpx.Response(403)
        return httpx.Response(403) if annotations_ok else httpx.Response(200, json={'steps': [{'name': 'Run docker/login-action@v3', 'conclusion': 'failure'}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    class GitHub:
        async def current_token(self):
            return 'test'
        def _rest_api_base(self):
            return 'https://api.github.test'
    result = await fetch_failure_evidence(GitHub(), 'org', 'repo', 33, 'https://untrusted.test/actions/runs/11/job/22')
    if annotations_ok:
        assert result['annotations'] == [RECORDED_193]
    else:
        assert result['setup_failure'] is True


def test_unrelated_evidence_requires_different_head_and_same_job_reason():
    from coordinare.services.failure_signature import make_failure_signature
    from coordinare.services.pr_checks_service import CheckEntry, CheckRollup, PrChecksService

    service = PrChecksService(object(), 'org', 'repo')
    check = CheckEntry(name='build', status='completed', conclusion='failure', summary='Shared service is broken')
    head = CheckRollup(pr_number=1, head_sha='a', base_ref='main', head_pushed_at=None, branch_protection_readable=False, checks=[check])
    signature, _ = make_failure_signature(check.name, check.conclusion, check.title, check.summary)
    service._observations.append(head)
    assert not service.unrelated_failure_seen(head, 'build', signature)
    service._observations.append(head.model_copy(update={'head_sha': 'b'}))
    assert not service.unrelated_failure_seen(head, 'build', signature)  # same PR history is not independent
    service._observations.append(head.model_copy(update={'head_sha': 'c', 'pr_number': 2}))
    assert service.unrelated_failure_seen(head, 'build', signature)
    assert not service.unrelated_failure_seen(head, 'other', signature)
    assert not service.unrelated_failure_seen(head, 'build', 'different-error')


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure", "action_required"])
async def test_fresh_independent_green_retries_current_failed_job_once(monkeypatch, conclusion):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from coordinare.services.pr_checks_service import CheckEntry, CheckRollup, PrChecksService

    class GitHub:
        async def current_token(self):
            return 'test'
        def _rest_api_base(self):
            return 'https://api.github.test'
    service = PrChecksService(GitHub(), 'org', 'repo')
    now = datetime.now(UTC)
    check = CheckEntry(name='build', status='completed', conclusion=conclusion, details_url='https://github.test/org/repo/actions/runs/11/job/22')
    head = CheckRollup(pr_number=1, head_sha='a', base_ref='main', head_pushed_at=None, branch_protection_readable=False, checks=[check])
    prior = {'head_sha': 'a', 'check_names': ['build'], 'blocked_at': now.isoformat()}
    green = check.model_copy(update={'conclusion': 'success', 'completed_at': now - timedelta(seconds=1)})
    service._observations.append(head.model_copy(update={'head_sha': 'b', 'pr_number': 2, 'checks': [green]}))
    assert await service.retry_recovered_infrastructure(head, prior) == []  # old green is not recovery
    green = green.model_copy(update={'completed_at': now + timedelta(seconds=1)})
    service._observations.append(head.model_copy(update={'head_sha': 'b', 'pr_number': 2, 'checks': [green]}))
    service.get_pr_check_rollup = AsyncMock(return_value=head)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200 if request.method == 'GET' else 201, json={'head_sha': 'a', 'conclusion': conclusion, 'status': 'completed'})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await service.retry_recovered_infrastructure(head, prior) == [22]
    assert await service.retry_recovered_infrastructure(head, prior) == []
    assert [request.method for request in requests] == ['GET', 'POST']
    assert requests[-1].url.path == '/repos/org/repo/actions/jobs/22/rerun'
    replacement = head.model_copy(update={'checks': [check.model_copy(update={'details_url': 'https://github.test/org/repo/actions/runs/11/job/23'})]})
    assert await service.retry_recovered_infrastructure(replacement, prior) == []
    # A real snapshot round trip must retain the one-retry budget even though
    # the rerun has a new Actions job ID and a fresh service has empty caches.
    from coordinare.daemon import _persist_active_sessions
    from coordinare.state_store import PersistedSession

    prior['pattern_id'] = 'registry_auth'
    persisted = _persist_active_sessions({'card': {'env_blocked': prior}})['card']
    restored = PersistedSession.model_validate_json(persisted.model_dump_json()).env_blocked
    assert restored == prior
    restarted_service = PrChecksService(GitHub(), 'org', 'repo')
    restarted_service._observations.extend(service._observations)
    assert await restarted_service.retry_recovered_infrastructure(replacement, restored) == []
    prior['retried_jobs'] = []
    prior['retried_checks'] = []
    service.get_pr_check_rollup.return_value = head.model_copy(update={'head_sha': 'new'})
    assert await service.retry_recovered_infrastructure(head, prior) == []
    assert len(requests) == 2


def test_notification_cooldown_and_failed_delivery_retry(monkeypatch):
    from coordinare.services.pr_checks_service import PrChecksService

    now = [100.0]
    monkeypatch.setattr('coordinare.services.pr_checks_service.time.monotonic', lambda: now[0])
    service = PrChecksService(object(), 'org', 'repo')
    assert service.claim_env_notification('same', 60)
    assert not service.claim_env_notification('same', 60)
    now[0] += 60
    assert service.claim_env_notification('same', 60)
    service.release_env_notification('same')
    assert service.claim_env_notification('same', 60)


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', [
    {'system_error_reason': 'ProxyLaunchError: startup probe failed'},
    {'env_blocked': {'cause': 'Runner unavailable'}},
    {'latest_ci_gate_decision': {'verdict': 'escalate'}},
])
async def test_system_block_has_operator_comment_without_fabricated_question(marker):
    from coordinare.graph.nodes.handle_blocked import handle_blocked
    from coordinare.graph.state import initial_state
    from tests.unit.graph.nodes.test_handle_blocked import _GitHubFallback

    github = _GitHubFallback()
    state = initial_state()
    state.update(marker)
    state.update(github_service=github, current_card={'id': 'ITEM_1', 'issue_id': 'ISSUE_1'}, open_questions=[])
    result = await handle_blocked(state)
    assert result['phase'] == 'blocked'
    assert github.moved_to == 'BLOCKED'
    assert 'System blocked' in github.comment_body
    assert 'Needs input' not in github.comment_body


@pytest.mark.asyncio
async def test_performer_remote_ci_hold_uses_shared_gate_without_invalidating_local_cache():
    from unittest.mock import Mock

    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer_ci_gate import (
        _ci_gate_state,
        _GitHubWithRollup,
        _rollup_payload,
    )

    github = _GitHubWithRollup(_rollup_payload(contexts=[{
        '__typename': 'CheckRun', 'name': 'build', 'status': 'COMPLETED', 'conclusion': 'FAILURE', 'title': RECORDED_193,
    }]))
    state = _ci_gate_state(github)
    state['symphony_configs']['default'].persona_scope.env_blocked_gate.enabled = True
    state['env_cache_service'] = Mock()
    response = state['performer_services']['implementing']._response
    response.update(status='env_blocked', reason=RECORDED_193, report={'ci_infrastructure': {
        'head_sha': 'a' * 40, 'check_names': ['build'], 'cause': 'Registry authentication failed',
    }})
    await monitor_performer(state)
    assert state['phase'] == 'monitoring_performer'
    assert state['agent_dispatch'] == {}
    state['env_cache_service'].mark_runtime_health_failed.assert_not_called()
    result = await monitor_performer(state)
    assert result['latest_ci_gate_decision']['verdict'] == 'hold'
    assert result['env_blocked']['check_names'] == ['build']
    assert result['bounce_counter'] == {}
    state['env_cache_service'].mark_runtime_health_failed.assert_not_called()


@pytest.mark.asyncio
async def test_pending_rerun_keeps_infrastructure_retry_budget_then_failure_stays_held():
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer_ci_gate import (
        _ci_gate_state,
        _GitHubWithRollup,
        _rollup_payload,
    )

    github = _GitHubWithRollup(_rollup_payload(contexts=[{
        '__typename': 'CheckRun', 'name': 'build', 'status': 'IN_PROGRESS', 'conclusion': None,
    }]))
    state = _ci_gate_state(github)
    state['symphony_configs']['default'].persona_scope.env_blocked_gate.enabled = True
    state['phase'] = 'monitoring_performer'
    state['agent_dispatch'] = {}
    state['current_card']['pr_url'] = 'https://github.com/org/repo/pull/42'
    state['env_blocked'] = {'head_sha': 'a' * 40, 'check_names': ['build'], 'retried_checks': ['build'],
                            'retried_jobs': [22], 'blocked_at': '2026-09-08T01:00:00+00:00'}
    await monitor_performer(state)
    assert state['latest_ci_gate_decision']['verdict'] == 'hold'
    assert state['env_blocked']['retried_checks'] == ['build']
    github._payload = _rollup_payload(contexts=[{
        '__typename': 'CheckRun', 'name': 'build', 'status': 'COMPLETED', 'conclusion': 'FAILURE', 'title': RECORDED_193,
    }])
    await monitor_performer(state)
    assert state['env_blocked']['retried_checks'] == ['build']
    assert state['env_blocked']['retried_jobs'] == [22]
    assert state['env_blocked']['blocked_at'] == '2026-09-08T01:00:00+00:00'
    assert state['bounce_counter'] == {}


@pytest.mark.parametrize('module_name', ['coordinare.services.ci_evidence', 'performer.ci_evidence'])
@pytest.mark.parametrize('rendered,template,expected', [
    ('Test ubuntu / 3.12', 'Test ${{ matrix.os }} / ${{ matrix.python }}', True),
    ('test (ubuntu, 3.12)', None, True),
    ('Other ubuntu', 'Test ${{ matrix.os }}', False),
    ('Product ubuntu', 'Test ${{ matrix.os }}', False),
])
def test_matrix_job_mapping_is_scoped_to_unique_job(module_name, rendered, template, expected):
    import importlib

    evidence = importlib.import_module(module_name)
    setup = {'strategy': {'matrix': {'os': ['ubuntu']}},
             'steps': [{'name': 'Prepare', 'uses': 'vendor/setup@v1'}, {'run': 'make test'}]}
    if template:
        setup['name'] = template
    product = {'name': 'Product ${{ matrix.os }}', 'steps': [{'name': 'Prepare', 'run': 'make test'}]}
    job = {'name': rendered, 'steps': [{'name': 'Prepare', 'conclusion': 'failure'}]}
    workflow = {'jobs': {'test': setup, 'product': product}}
    assert evidence.failed_step_context(job, workflow) == (['Prepare'], expected)
    if expected:
        workflow['jobs']['duplicate'] = dict(setup, name=rendered)
        assert evidence.failed_step_context(job, workflow) == (['Prepare'], False)


@pytest.mark.asyncio
@pytest.mark.parametrize('module_name', ['coordinare.services.ci_evidence', 'performer.ci_evidence'])
@pytest.mark.parametrize('message,expected_count,expected_chars', [('x' * 4000, 8, 32000), ('x', 50, 50)])
async def test_annotation_collection_stops_at_total_or_count_budget(monkeypatch, module_name, message, expected_count, expected_chars):
    import importlib

    evidence = importlib.import_module(module_name)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[{'annotation_level': 'failure', 'message': message}] * 100)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    class GitHub:
        async def current_token(self):
            return 'test'
        def _rest_api_base(self):
            return 'https://api.github.test'
    args = (GitHub(),) if module_name.startswith('coordinare') else ('https://api.github.test', 'test')
    result = await evidence.fetch_failure_evidence(*args, 'org', 'repo', 33, None)
    assert len(result['annotations']) == expected_count
    assert sum(map(len, result['annotations'])) == expected_chars
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_recovery_does_not_retry_a_job_that_is_already_running(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, Mock

    from coordinare.services.pr_checks_service import CheckEntry, CheckRollup, PrChecksService

    github = Mock()
    github.current_token = AsyncMock(return_value="test")
    github._rest_api_base.return_value = "https://api.github.test"
    service = PrChecksService(github, "org", "repo")
    check = CheckEntry(name="build", status="completed", conclusion="failure",
                       details_url="https://github.test/org/repo/actions/runs/11/job/22")
    rollup = CheckRollup(pr_number=1, head_sha="a", base_ref="main", head_pushed_at=None,
                        branch_protection_readable=False, checks=[check])
    service.get_pr_check_rollup = AsyncMock(return_value=rollup)
    service.infrastructure_recovered = Mock(return_value=True)
    prior = {"head_sha": "a", "check_names": ["build"], "blocked_at": datetime.now(UTC).isoformat()}
    requests = []
    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"head_sha": "a", "status": "in_progress", "conclusion": "failure"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await service.retry_recovered_infrastructure(rollup, prior) == []
    assert requests == ["GET"]
    assert "retried_jobs" not in prior


@pytest.mark.asyncio
async def test_plain_rollup_does_not_fetch_infrastructure_evidence(monkeypatch):
    from unittest.mock import AsyncMock

    from coordinare.services.pr_checks_service import PrChecksService
    from tests.unit.graph.nodes.test_monitor_performer_ci_gate import (
        _GitHubWithRollup,
        _rollup_payload,
    )

    evidence = AsyncMock(return_value={
        "annotations": [RECORDED_193], "failed_steps": [], "setup_failure": True,
    })
    monkeypatch.setattr("coordinare.services.ci_evidence.fetch_failure_evidence", evidence)
    github = _GitHubWithRollup(_rollup_payload(contexts=[{
        "__typename": "CheckRun", "name": "Build", "databaseId": 193,
        "status": "COMPLETED", "conclusion": "FAILURE", "title": "Failed", "summary": "",
    }]))
    service = PrChecksService(github, "org", "repo")
    rollup = await service.get_pr_check_rollup(1)
    evidence.assert_not_awaited()
    enriched = await service.enrich_failure_evidence(rollup)
    evidence.assert_awaited_once()
    assert enriched.checks[0].setup_failure
