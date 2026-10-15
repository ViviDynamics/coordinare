"""#143 third check: the server says conflict where it is one, and refuses duplicate routing pairs.

Written before the fix. A stale base hash is a version conflict and must carry the flag the
page reads; a create or update that makes a duplicate (backend, model) pair is refused with a
422; a pair that was already duplicated on disk does not block an unrelated write; a malformed
`expected` is a 422, and only an absent one skips the identity check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.unit.test_dashboard_config_api import _make_client, _routing_hash

_STALE = "sha256:" + "0" * 64


@pytest.fixture(autouse=True)
def _github_token_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures keep ${COORDINARE_GITHUB_TOKEN} in the file; the daemon reads it from the environment (225)."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_realtoken")


def _client(config_path: Path) -> TestClient:
    return _make_client(config_path, yaml.safe_load(config_path.read_text()))


def _entry(backend: str, model: str) -> dict:
    return {"backend": backend, "model": model, "target": {
        "base_url": "http://localhost:8000/v1", "wire_format": "openai", "strategy": "normalize",
        "normalizers": ["harmony_tool_calls"]}}


def _write_routing(routing_path: Path, entries: list[dict]) -> None:
    routing_path.write_text(yaml.safe_dump({"entries": entries}, sort_keys=False))


def test_a_stale_section_save_is_a_conflict_the_page_can_reload(temp_config_path: Path) -> None:
    client = _client(temp_config_path)
    resp = client.put("/api/config/section/global",
                      json={"store": "config_yaml", "section": "global",
                            "changes": {"poll_interval_seconds": 45}, "base_hash": _STALE})
    assert resp.status_code == 409
    assert resp.json().get("conflict") is True


def test_a_stale_routing_save_is_a_conflict_the_page_can_reload(temp_config_with_routing) -> None:
    config_path, _routing_path = temp_config_with_routing
    client = _client(config_path)
    resp = client.put("/api/config/routing/entry/0",
                      json={"expected": {"backend": "vllm", "model": "qwen2.5-coder"},
                            "changes": {"model": "x"}, "base_hash": _STALE})
    assert resp.status_code == 409
    assert resp.json().get("conflict") is True


def test_a_create_that_makes_a_duplicate_pair_is_refused(temp_config_with_routing) -> None:
    config_path, routing_path = temp_config_with_routing
    client = _client(config_path)
    resp = client.post("/api/config/routing/entry",
                       json={"entry": _entry("vllm", "qwen2.5-coder"), "base_hash": _routing_hash(client)})
    assert resp.status_code == 422
    assert "already" in resp.json()["errors"][0]["message"]
    assert len(yaml.safe_load(routing_path.read_text())["entries"]) == 1


def test_an_update_that_makes_a_duplicate_pair_is_refused(temp_config_with_routing) -> None:
    config_path, routing_path = temp_config_with_routing
    _write_routing(routing_path, [_entry("vllm", "a"), _entry("vllm", "b")])
    client = _client(config_path)
    resp = client.put("/api/config/routing/entry/1",
                      json={"changes": {"model": "a"}, "base_hash": _routing_hash(client)})
    assert resp.status_code == 422
    assert [e["model"] for e in yaml.safe_load(routing_path.read_text())["entries"]] == ["a", "b"]


def test_an_unrelated_write_to_a_table_with_a_duplicate_on_disk_lands_on_its_own_entry(temp_config_with_routing) -> None:
    """A duplicate already on disk does not block an unrelated write: the write lands on the entry it names."""
    config_path, routing_path = temp_config_with_routing
    _write_routing(routing_path, [_entry("vllm", "a"), _entry("vllm", "a")])
    client = _client(config_path)
    resp = client.put("/api/config/routing/entry/1",
                      json={"changes": {"target": {"base_url": "http://elsewhere"}},
                            "base_hash": _routing_hash(client)})
    assert resp.status_code == 200
    entries = yaml.safe_load(routing_path.read_text())["entries"]
    assert entries[0]["target"]["base_url"] == "http://localhost:8000/v1"
    assert entries[1]["target"]["base_url"] == "http://elsewhere"


def test_a_malformed_expected_is_a_validation_error_and_an_absent_one_is_not_checked(temp_config_with_routing) -> None:
    config_path, _routing_path = temp_config_with_routing
    client = _client(config_path)
    bad = client.put("/api/config/routing/entry/0",
                     json={"expected": "vllm", "changes": {"model": "x"}, "base_hash": _routing_hash(client)})
    assert bad.status_code == 422
    absent = client.put("/api/config/routing/entry/0",
                        json={"changes": {"model": "x"}, "base_hash": _routing_hash(client)})
    assert absent.status_code == 200
