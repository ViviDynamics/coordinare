"""Integration tests for the spec-081 config UI write path (T023, T048).

These exercise the full dashboard save flow end-to-end against a temp
``config.yaml`` fixture:

  * T023 — a successful section save persists to disk, recomputes a fresh
    content hash, and fires the daemon's hot-reload trigger so the running
    config picks up the change (write → reload → reflect).
  * T048 — FR-015 reload-failure integrity: when the atomic write succeeds but
    the live hot-reload fails, the prior in-memory config is retained (no partial
    swap), the result downgrades to ``staged_restart`` with an operator-readable
    message, and no secret leaks into the message.

The coordinare and performer suites run separately (conftest collision); these
tests are hermetic and use a MagicMock daemon — no live performer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app

if TYPE_CHECKING:
    from pathlib import Path


def _make_client(config_path: Path, raw: dict[str, Any]) -> tuple[TestClient, MagicMock]:
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    coordinare_cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": coordinare_cfg, "config_version": 7}
    daemon.running = True
    # A real-ish reload trigger so we can assert it fired / inject a failure.
    daemon._config_reload_trigger = MagicMock()
    daemon._webhook_trigger = MagicMock()
    app = create_dashboard_app(
        DashboardStore(),
        daemon,
        MagicMock(),
        MagicMock(),
        config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090"), daemon


def _hash(client: TestClient) -> str:
    return client.get("/api/config/all").json()["content_hashes"]["config_yaml"]


# --- T023: write → reload → reflect -------------------------------------------


def test_section_save_writes_and_triggers_reload(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client, daemon = _make_client(temp_config_path, raw)
    base_hash = _hash(client)

    resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"poll_interval_seconds": 45},
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == "hot_reloaded"

    # Persisted to disk.
    assert yaml.safe_load(temp_config_path.read_text())["poll_interval_seconds"] == 45
    # A fresh hash is returned and matches the new on-disk content.
    assert body["new_hash"] == _hash(client)
    assert body["new_hash"] != base_hash
    # The daemon was told to hot-reload.
    daemon._config_reload_trigger.set.assert_called()


# --- T048: FR-015 reload-failure integrity ------------------------------------


def test_reload_failure_retains_prior_config_and_stages_restart(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client, daemon = _make_client(temp_config_path, raw)
    prior_config = daemon.state["coordinare_config"]
    prior_version = daemon.state["config_version"]
    base_hash = _hash(client)

    # Injected hot-reload failure after the successful atomic write.
    daemon._config_reload_trigger.set.side_effect = RuntimeError("reload boom")

    resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"poll_interval_seconds": 51},
            "base_hash": base_hash,
        },
    )
    # The write itself succeeded; reload failing does not turn it into an error.
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # Downgraded from hot_reloaded → staged_restart with an operator-readable note.
    assert body["applied"] == "staged_restart"
    assert body["message"]
    assert "restart" in body["message"].lower()
    # Secret-free message.
    assert "ghp_" not in body["message"]
    assert "Traceback" not in body["message"]

    # The change IS on disk (atomic write happened before the reload attempt).
    assert yaml.safe_load(temp_config_path.read_text())["poll_interval_seconds"] == 51
    # But the in-memory running config is unchanged — no partial swap, no version bump.
    assert daemon.state["coordinare_config"] is prior_config
    assert daemon.state["config_version"] == prior_version


# --- Catalog CRUD reload-failure integrity (Copilot round 13) -----------------
#
# FR-015 reload-failure integrity also applies to the catalog CRUD endpoints:
# when the atomic write succeeds but the live hot-reload trigger raises, the
# response must downgrade from ``hot_reloaded`` to ``staged_restart`` with a
# secret-free operator advisory (mirroring ``put_config_section``), not falsely
# claim the change is live.


def _config_yaml_hash(client: TestClient) -> str:
    return client.get("/api/config/all").json()["content_hashes"]["config_yaml"]


def test_catalog_post_reload_failure_stages_restart(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client, daemon = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    daemon._config_reload_trigger.set.side_effect = RuntimeError("reload boom")

    resp = client.post(
        "/api/config/catalog/endpoints",
        json={
            "item": {"name": "extra-vllm", "kind": "vllm", "base_url": "http://h:8001"},
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == "staged_restart"
    assert body["message"]
    assert "restart" in body["message"].lower()
    assert "ghp_" not in body["message"]
    assert "Traceback" not in body["message"]
    # The change is still persisted on disk.
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "extra-vllm" in names


def test_catalog_put_reload_failure_stages_restart(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client, daemon = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    daemon._config_reload_trigger.set.side_effect = RuntimeError("reload boom")

    resp = client.put(
        "/api/config/catalog/endpoints/openai-native",
        json={"changes": {"auth_env": "OPENAI_KEY_2"}, "base_hash": base_hash},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == "staged_restart"
    assert "restart" in body["message"].lower()
    assert "ghp_" not in body["message"]


def test_catalog_delete_reload_failure_stages_restart(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client, daemon = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    daemon._config_reload_trigger.set.side_effect = RuntimeError("reload boom")

    resp = client.request(
        "DELETE",
        "/api/config/catalog/endpoints/openai-native",
        json={"base_hash": base_hash},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == "staged_restart"
    assert "restart" in body["message"].lower()
    assert "ghp_" not in body["message"]


# --- T042: performance budget (SC-010) ----------------------------------------


def _p95(samples: list[float]) -> float:
    """95th-percentile of ``samples`` (seconds), nearest-rank."""
    ordered = sorted(samples)
    # nearest-rank index: ceil(0.95 * n) - 1, clamped to the last element.
    idx = min(len(ordered) - 1, max(0, (95 * len(ordered) + 99) // 100 - 1))
    return ordered[idx]


def test_get_config_all_under_300ms_p95(temp_config_path):
    """SC-010: ``GET /api/config/all`` server-side p95 stays under 300 ms.

    Measured against the in-process TestClient (no network) so the sample is the
    server-side cost: descriptor build + secret masking + content-hashing.
    """
    import time

    raw = yaml.safe_load(temp_config_path.read_text())
    client, _daemon = _make_client(temp_config_path, raw)

    # Warm up import/JIT-ish caches so the percentile reflects steady state.
    client.get("/api/config/all")

    samples: list[float] = []
    for _ in range(30):
        start = time.perf_counter()
        resp = client.get("/api/config/all")
        samples.append(time.perf_counter() - start)
        assert resp.status_code == 200

    p95 = _p95(samples)
    assert p95 < 0.300, f"GET /api/config/all p95 was {p95 * 1000:.1f}ms (budget 300ms)"


def test_section_save_round_trip_under_500ms_p95(temp_config_path):
    """SC-010: a single section save round-trip server-side p95 stays under 500 ms.

    Each iteration re-reads the baseline hash (a save invalidates the prior one)
    then issues the guarded atomic write, so the sample covers the full
    read-hash → validate → atomic-write → reload-trigger path.
    """
    import time

    raw = yaml.safe_load(temp_config_path.read_text())
    client, _daemon = _make_client(temp_config_path, raw)

    # Warm up one full save round-trip (imports, pydantic schema caches, YAML
    # emitter setup) so the percentile reflects steady state — parity with the
    # GET benchmark above.
    warm_resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"poll_interval_seconds": 30},
            "base_hash": _hash(client),
        },
    )
    assert warm_resp.status_code == 200

    samples: list[float] = []
    for i in range(20):
        base_hash = _hash(client)
        start = time.perf_counter()
        resp = client.put(
            "/api/config/section/global",
            json={
                "store": "config_yaml",
                "section": "global",
                "changes": {"poll_interval_seconds": 30 + (i % 10)},
                "base_hash": base_hash,
            },
        )
        samples.append(time.perf_counter() - start)
        assert resp.status_code == 200

    p95 = _p95(samples)
    assert p95 < 0.500, f"section save p95 was {p95 * 1000:.1f}ms (budget 500ms)"
