"""#143 second review: routing writes name the entry they mean, and the server refuses a stale index.

Written before the fix. A write that carries the backend and model of the entry it means must
be refused, with a conflict, when the entry at that index is a different one.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from tests.unit.test_dashboard_config_api import _make_client, _routing_hash


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    return _make_client(config_path, raw)


def test_routing_put_refuses_an_index_that_now_holds_another_entry(temp_config_with_routing) -> None:
    config_path, routing_path = temp_config_with_routing
    client = _client(config_path)
    resp = client.put(
        "/api/config/routing/entry/0",
        json={"expected": {"backend": "not-vllm", "model": "gone-model"},
              "changes": {"model": "overwritten"}, "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 409
    assert resp.json()["conflict"] is True
    assert yaml.safe_load(routing_path.read_text())["entries"][0]["model"] == "qwen2.5-coder"


def test_routing_delete_refuses_an_index_that_now_holds_another_entry(temp_config_with_routing) -> None:
    config_path, routing_path = temp_config_with_routing
    client = _client(config_path)
    resp = client.request(
        "DELETE",
        "/api/config/routing/entry/0",
        json={"expected": {"backend": "not-vllm", "model": "gone-model"}, "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 409
    assert resp.json()["conflict"] is True
    assert len(yaml.safe_load(routing_path.read_text())["entries"]) == 1


def test_routing_put_applies_when_the_named_entry_is_at_that_index(temp_config_with_routing) -> None:
    config_path, routing_path = temp_config_with_routing
    client = _client(config_path)
    resp = client.put(
        "/api/config/routing/entry/0",
        json={"expected": {"backend": "vllm", "model": "qwen2.5-coder"},
              "changes": {"model": "qwen2.5-coder-32b"}, "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 200
    assert yaml.safe_load(routing_path.read_text())["entries"][0]["model"] == "qwen2.5-coder-32b"
