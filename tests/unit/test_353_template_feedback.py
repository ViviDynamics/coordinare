"""Reject root-template mistakes before execution and diagnose ambiguous JSON."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    EnvironmentDiscoveryError,
    _decode_reply,
)
from tests.unit.test_351_environment_discovery import PATH
from tests.unit.test_353_discovery_repair import Replies, assessment, candidate, run


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['${cache}/tool', '${scratch}/output', '${repo}/project', '${ver}'])
async def test_template_preflight_prevents_any_install_side_effects(tmp_path, monkeypatch, value):
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.test_351_environment_runner import contract

    for name in ('cache', 'scratch', 'repo', 'ver'):
        monkeypatch.delenv(name, raising=False)
    marker = tmp_path / 'side-effect'
    manifest = contract([{'id': 'baseline', 'argv': ['echo', value]}], install=[
        {'id': 'producer', 'argv': ['touch', str(marker)]}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True)
    assert not marker.exists()
    assert not report.passed
    failure = next(result for result in report.results if result.status == 'fail')
    assert failure.id == 'baseline'
    assert 'unresolved environment reference' in failure.output
    assert '{cache}' in failure.output and '$name' in failure.output
    assert next(result for result in report.results if result.id == 'producer').status == 'not_run'


@pytest.mark.asyncio
async def test_template_preflight_preserves_runtime_environment_and_shell_variables(tmp_path, monkeypatch):
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.test_351_environment_runner import contract

    monkeypatch.setenv('repo', str(tmp_path))
    manifest = contract([{'id': 'baseline', 'argv': ['bash', '-c', 'ver=1; test "$ver" = 1; test "${repo}" = "{repo}"']}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert report.passed


@pytest.mark.parametrize('raw', ['{"assessment":{},"assessment":true}', '{"assessment":{"verdict":"accept","verdict":"revise"}}'])
def test_duplicate_json_has_specific_safe_code(raw):
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        _decode_reply(raw, 'review', DiscoveryLimits())
    assert caught.value.code == 'duplicate_json_key'


@pytest.mark.asyncio
async def test_duplicate_review_can_only_repair_with_a_new_complete_assessment():
    valid = candidate()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': valid},
                       {'tool': 'read_file', 'path': PATH}])
    calls = 0
    reviewer_messages = []

    async def chat(messages):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return await replies(messages)
        if calls == 4:
            return '{"assessment":{"verdict":"accept"},"assessment":true}'
        reviewer_messages.extend(messages)
        return json.dumps(assessment(valid))

    events = []
    manifest, review = await run(chat, diagnostics=events.append)
    assert review.manifest_digest == manifest.digest()
    feedback = json.loads(reviewer_messages[-1]['content'])
    assert feedback['validation_errors'][0]['code'] == 'duplicate_json_key'
    assert 'unique keys' in feedback['validation_errors'][0]['guidance']
    assert events[-1]['schema_repairs'] == 1


@pytest.mark.asyncio
async def test_duplicate_review_never_bypasses_exhausted_schema_allowance():
    from coordinare.services.environment_attempt import AttemptBudget

    valid = candidate()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': valid},
                       {'tool': 'read_file', 'path': PATH}])
    calls = 0

    async def chat(messages):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return await replies(messages)
        return '{"assessment":{"verdict":"accept"},"assessment":true}'

    budget = AttemptBudget(max_schema_repairs=0)
    with pytest.raises(EnvironmentDiscoveryError, match='repair limit'):
        await run(chat, budget=budget)
    assert calls == 4
    assert budget.validation_codes[-1] == "duplicate_json_key"


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['argv', 'env', 'cwd', 'activation'])
async def test_actual_target_preflight_covers_all_expansion_fields(tmp_path, monkeypatch, field):
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.test_351_environment_runner import contract

    monkeypatch.delenv('NOT_PROVIDED', raising=False)
    check = {'id': 'baseline', 'argv': ['true']}
    changes = {}
    if field == 'activation':
        changes['activation'] = {'env': {'CUSTOM': '${NOT_PROVIDED}'}}
    else:
        check[field] = {'CUSTOM': '${NOT_PROVIDED}'} if field == 'env' else '${NOT_PROVIDED}' if field == 'cwd' else ['echo', '${NOT_PROVIDED}']
    marker = tmp_path / 'effect'
    manifest = contract([check], install=[{'id': 'producer', 'argv': ['touch', str(marker)]}], **changes)
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True)
    assert not report.passed
    assert not marker.exists()


@pytest.mark.asyncio
async def test_template_preflight_respects_environment_order_and_scope(tmp_path):
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.test_351_environment_runner import contract

    manifest = contract([
        {'id': 'qa-only', 'scope': ['qa'], 'argv': ['echo', '${UNDEFINED_QA}']},
        {'id': 'baseline', 'argv': ['test', '${SECOND}', '=', '{repo}/first/second'],
         'env': {'SECOND': '${FIRST}/second', 'FIRST': '{repo}/first'}},
        {'id': 'optional', 'required': False, 'argv': ['echo', '${UNDEFINED_OPTIONAL}']},
    ])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, scope='dev')
    assert report.passed
    assert next(result for result in report.results if result.id == 'baseline').status == 'pass'


def test_template_preflight_keeps_aggregate_output_bounded(monkeypatch):
    from coordinare_service_inference.environment.runner import (
        ATTEMPT_OUTPUT_LIMIT,
        _template_preflight,
    )

    from tests.unit.test_351_environment_runner import contract

    monkeypatch.delenv('MISSING', raising=False)
    manifest = contract([{'id': f'check-{index}', 'argv': ['echo', '${MISSING}', str(index)]} for index in range(1250)])
    report = _template_preflight(manifest, {'repo': '/repo', 'cache': '/cache', 'scratch': '/scratch'},
                                 install=True, scope='', secret_names=())
    assert report is not None and not report.passed
    assert sum(len(result.output.encode()) for result in report.results) <= ATTEMPT_OUTPUT_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('env', {'DATABASE_URL': 'postgresql://user:PRIVATE_SENTINEL@localhost/test'}),
    ('env', {'ACCESS_PASSWORD': 'PRIVATE_SENTINEL'}),
    ('argv', ['tool', '--url', 'postgresql://user:PRIVATE_SENTINEL@localhost/test']),
])
async def test_credential_repair_feedback_explains_runtime_reference_without_exposing_values(field, value):
    import copy

    from coordinare.services.environment_attempt import AttemptBudget

    valid = candidate()
    valid['checks'][0][field] = ({'DATABASE_URL': '${DATABASE_URL}'} if field == 'env'
                                else ['tool', '--url', '${DATABASE_URL}'])
    invalid = copy.deepcopy(valid)
    invalid['checks'][0][field] = value
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': invalid},
                       {'manifest': valid}, {'tool': 'read_file', 'path': PATH}, assessment(valid)])
    feedback = []
    async def chat(messages):
        for message in messages:
            try:
                content = json.loads(message['content'])
            except ValueError:
                continue
            if isinstance(content, dict) and 'validation_errors' in content:
                feedback.extend(content['validation_errors'])
        return await replies(messages)
    budget = AttemptBudget()
    manifest, review = await run(chat, budget=budget)
    error = next(item for item in feedback if item['location'] == ['checks', 0, field])
    assert error['code'] == 'runtime_credential_reference'
    assert '${NAME}' in error['guidance'] and 'argv' in error['guidance']
    assert 'PRIVATE_SENTINEL' not in json.dumps(feedback)
    assert budget.schema_repairs == 1
    assert manifest.checks[0].model_dump()[field] == valid['checks'][0][field]
    assert review.manifest_digest == manifest.digest()
