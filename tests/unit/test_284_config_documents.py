from __future__ import annotations

import pytest

from coordinare.config import SymphonyConfig
from coordinare.services.config_documents import symphony_documents


@pytest.mark.parametrize('raw', [{}, {'symphonies': None}])
def test_legacy_documents_without_symphony_list_accept_first_write(raw: dict) -> None:
    cfg = SymphonyConfig(name='default', github_project_number=42)
    documents = symphony_documents({'default': cfg}, raw)
    assert documents[0]['name'] == 'default'
    assert documents[0]['github_project_number'] == 42


def test_reordered_named_channels_keep_their_own_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FIRST_SECRET', 'same-value')
    monkeypatch.setenv('SECOND_SECRET', 'same-value')
    raw = {'symphonies': [{'name': 'demo', 'overrides': {'notifications': {'channels': [
        {'name': 'first', 'webhook_url': '${FIRST_SECRET}'},
        {'name': 'second', 'webhook_url': '${SECOND_SECRET}'},
    ]}}}]}
    config = SymphonyConfig(name='demo', github_project_number=42, overrides={'notifications': {'channels': [
        {'name': 'second', 'webhook_url': 'same-value'},
        {'name': 'first', 'webhook_url': 'same-value'},
    ]}})
    documents = symphony_documents({'demo': config}, raw)
    channels = documents[0]['overrides']['notifications']['channels']
    assert channels[0]['webhook_url'] == '${SECOND_SECRET}'
    assert channels[1]['webhook_url'] == '${FIRST_SECRET}'
