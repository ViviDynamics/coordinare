"""163: policy is opt-in and belongs to each resolved model."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coordinare.config import ModelEndpoint, ProjectConfiguration


def policy_config(tmp_path, monkeypatch, policy=None, strategy='single'):
    monkeypatch.setattr(Path, 'cwd', lambda: tmp_path)
    model = {'name': 'tool', 'endpoint': 'local', 'model': 'ada/qwen3-14b'}
    if policy:
        model['reasoning_policy'] = policy
    mode = {'name': 'mode', 'strategy': strategy, 'tool': 'tool'}
    if strategy != 'single':
        mode['thinking'] = 'think'
    return ProjectConfiguration.model_validate({
        'project_name': 'test', 'github_org': 'org', 'github_project_number': 1,
        'github_token': 'test', 'human_reviewers': ['reviewer'],
        'endpoints': [{'name': 'local', 'kind': 'litellm', 'base_url': 'http://gateway/v1', 'auth_env': 'TEST_KEY'}],
        'model_endpoints': [model, {'name': 'think', 'endpoint': 'local', 'model': 'test-vendor/glm-5.3-flash'}],
        'modes': [mode], 'performers': {'reviewer': {'backend': 'codex', 'mode': 'mode'}},
    })


def test_absent_policy_leaves_dispatch_and_single_routing_unchanged(tmp_path, monkeypatch):
    config = policy_config(tmp_path, monkeypatch)
    assert config.resolve_performer_dispatch_model('reviewer') == {
        'model': 'ada/qwen3-14b', 'base_url': 'http://gateway/v1', 'auth_token_env': 'TEST_KEY',
    }
    assert config.resolve_performer_orchestration('reviewer') is None


@pytest.mark.parametrize('strategy', ['single', 'always'])
def test_policy_reaches_executor_without_affecting_planner(tmp_path, monkeypatch, strategy):
    config = policy_config(tmp_path, monkeypatch, 'disable_thinking', strategy)
    assert config.resolve_performer_dispatch_model('reviewer')['reasoning_policy'] == 'disable_thinking'
    block = config.resolve_performer_orchestration('reviewer')
    assert block['tool']['reasoning_policy'] == 'disable_thinking'
    if strategy != 'single':
        assert 'reasoning_policy' not in block['thinking']


def test_unknown_policy_names_are_rejected():
    with pytest.raises(ValidationError, match='invented'):
        ModelEndpoint(name='m', endpoint='e', model='qwen', reasoning_policy='invented')


def test_native_endpoint_rejects_self_hosted_policy(tmp_path, monkeypatch):
    config = policy_config(tmp_path, monkeypatch, 'disable_thinking')
    data = config.model_dump()
    data['endpoints'] = [{'name': 'local', 'kind': 'openai'}]
    with pytest.raises(ValidationError, match='requires a self-hosted endpoint'):
        ProjectConfiguration.model_validate(data)
