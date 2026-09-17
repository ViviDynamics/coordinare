"""Contract tests for the spec-081 config UI API (T011).

Covers ``GET /api/config/all`` and ``GET /api/config/section/{section_id}``:
the response shape, presence of all 7 sections (data-model E2), secret masking /
``${VAR}`` preservation (research D3), the ``content_hashes`` baselines (research
D4), and 404 on an unknown section id. Existing ``/api/config/*`` endpoints are
preserved — exercised elsewhere in ``test_dashboard.py``.

The coordinare and performer suites run separately (conftest collision); these
tests are hermetic against a temp ``config.yaml`` fixture, no live performer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_descriptors import SECRET_MASK
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app


def _version_header(config_path) -> dict[str, str]:
    """The ``If-Match`` a write needs after spec 158 (#241).

    Every route that writes ``config.yaml`` now requires the version it is writing
    against, so a concurrent edit is refused rather than silently overwritten.
    """
    from coordinare.services.config_write_service import compute_content_hash

    return {"If-Match": compute_content_hash(config_path)}


if TYPE_CHECKING:
    from pathlib import Path

SECTION_IDS = {
    "global",
    "personas",
    "symphonies",
    "endpoints",
    "model_endpoints",
    "modes",
    "routing",
}


def _make_client(config_path: Path, raw: dict[str, Any]) -> TestClient:
    # The validated config object can't hold the on-disk ${VAR} github_token
    # (a validator rejects unresolved placeholders for pat auth — expansion
    # happens only at load). The endpoint sources display values from the raw
    # on-disk YAML, so substitute a real token here for the validated object.
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    coordinare_cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {
        "coordinare_config": coordinare_cfg,
        "config_version": 7,
    }
    daemon.running = True
    app = create_dashboard_app(
        DashboardStore(),
        daemon,
        MagicMock(),
        MagicMock(),
        config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


# --- GET /api/config/all ------------------------------------------------------


def test_config_all_returns_all_seven_sections(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/all")
    assert resp.status_code == 200
    body = resp.json()
    assert {s["id"] for s in body["sections"]} == SECTION_IDS


def test_config_all_includes_content_hashes_and_version(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    body = client.get("/api/config/all").json()
    assert body["config_version"] == 7
    assert "config_yaml" in body["content_hashes"]
    assert body["content_hashes"]["config_yaml"].startswith("sha256:")


def test_config_all_masks_secret_but_preserves_env_placeholder(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    body = client.get("/api/config/all").json()
    global_section = next(s for s in body["sections"] if s["id"] == "global")
    token = next(
        s for s in global_section["settings"] if s["key"] == "global.github_token"
    )
    # The fixture token is a ${VAR} literal — preserved raw, never masked.
    assert token["is_env_placeholder"] is True
    assert token["current_value"] == "${COORDINARE_GITHUB_TOKEN}"
    assert token["current_value"] != SECRET_MASK


def test_config_all_routing_unavailable_empty_state(temp_config_path):
    # The plain REPRESENTATIVE_CONFIG mounts a routing volume at a bogus host path
    # that does not exist on disk, so routing is unavailable.
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    body = client.get("/api/config/all").json()
    assert body["routing_available"] is False
    routing = next(s for s in body["sections"] if s["id"] == "routing")
    assert routing["items"] == []
    assert routing["invalid_banner"] is not None


# --- GET /api/config/section/{section_id} -------------------------------------


def test_config_section_returns_single_section(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/section/global")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "global"
    assert body["kind"] == "scalar_group"


def test_config_section_unknown_returns_404(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/section/does_not_exist")
    assert resp.status_code == 404


# --- PUT /api/config/section/{section_id} (T020) ------------------------------


def _config_yaml_hash(client: TestClient) -> str:
    return client.get("/api/config/all").json()["content_hashes"]["config_yaml"]


def test_put_section_global_hot_reloaded(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
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
    assert body["new_hash"].startswith("sha256:")
    assert body["errors"] == []
    # The change is persisted to disk.
    assert yaml.safe_load(temp_config_path.read_text())["poll_interval_seconds"] == 45


def test_put_section_restart_required_field_staged(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"dashboard_port": 9123},
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] == "staged_restart"


def test_put_section_validation_error_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"poll_interval_seconds": 999999},  # out of range (le=3600)
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["errors"]
    err = body["errors"][0]
    assert err["code"] == "validation"
    assert err["key"] == "poll_interval_seconds"
    # No stack traces / pydantic dumps leak through.
    assert "Traceback" not in err["message"]


def test_put_section_conflict_409(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.put(
        "/api/config/section/global",
        json={
            "store": "config_yaml",
            "section": "global",
            "changes": {"poll_interval_seconds": 45},
            "base_hash": "sha256:deadbeef",  # stale / wrong baseline
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "conflict"


# --- Catalog CRUD (T021) ------------------------------------------------------


def test_catalog_get_endpoints_with_refs(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/catalog/endpoints")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_hash"].startswith("sha256:")
    by_id = {i["id"]: i for i in body["items"]}
    # local-vllm is referenced by the qwen-coder model_endpoint → not deletable.
    assert by_id["local-vllm"]["referenced_by"] == ["qwen-coder"]
    assert by_id["local-vllm"]["deletable"] is False
    # openai-native is referenced by nothing → deletable.
    assert by_id["openai-native"]["deletable"] is True


def test_catalog_post_create_endpoint(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.post(
        "/api/config/catalog/endpoints",
        json={
            "item": {"name": "extra-vllm", "kind": "vllm", "base_url": "http://h:8001"},
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "extra-vllm" in names


def test_catalog_post_unknown_reference_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.post(
        "/api/config/catalog/model_endpoints",
        json={
            "item": {"name": "bad", "endpoint": "no-such-endpoint", "model": "m"},
            "base_hash": base_hash,
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"][0]["code"] == "validation"
    assert "no-such-endpoint" in body["errors"][0]["message"]


def test_catalog_put_update_endpoint(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.put(
        "/api/config/catalog/endpoints/openai-native",
        json={"changes": {"auth_env": "OPENAI_KEY_2"}, "base_hash": base_hash},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    eps = {e["name"]: e for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]}
    assert eps["openai-native"]["auth_env"] == "OPENAI_KEY_2"


def test_catalog_delete_referenced_409(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.request(
        "DELETE",
        "/api/config/catalog/endpoints/local-vllm",
        json={"base_hash": base_hash},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["errors"][0]["code"] == "referenced"
    # The error names the referrer.
    assert "qwen-coder" in body["errors"][0]["message"]


def test_catalog_delete_unreferenced_ok(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    resp = client.request(
        "DELETE",
        "/api/config/catalog/endpoints/openai-native",
        json={"base_hash": base_hash},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "openai-native" not in names


# --- Routing CRUD (T034, spec-078) --------------------------------------------


def _routing_hash(client: TestClient) -> str:
    return client.get("/api/config/routing").json()["content_hash"]


def _routing_entry(model: str = "llama-3") -> dict[str, Any]:
    return {
        "backend": "vllm",
        "model": model,
        "target": {
            "base_url": "http://localhost:8000/v1",
            "wire_format": "openai",
            "strategy": "normalize",
            "normalizers": ["harmony_tool_calls"],
        },
    }


def test_routing_get_unavailable_empty_state(temp_config_path):
    # REPRESENTATIVE_CONFIG mounts a routing volume at a host path that doesn't exist.
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/routing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["routing_available"] is False
    assert body["entries"] == []
    assert body["content_hash"] is None


def test_routing_get_available_returns_entries(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.get("/api/config/routing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["routing_available"] is True
    assert body["content_hash"].startswith("sha256:")
    assert len(body["entries"]) == 1
    assert body["entries"][0]["model"] == "qwen2.5-coder"


def test_routing_post_create_staged_next_job(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.post(
        "/api/config/routing/entry",
        json={"entry": _routing_entry("llama-3"), "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == "staged_next_job"
    models = {e["model"] for e in yaml.safe_load(routing_path.read_text())["entries"]}
    assert "llama-3" in models


def test_routing_post_invalid_422(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    bad = _routing_entry()
    bad["target"]["strategy"] = "reroute"  # reroute + normalizers → invalid
    resp = client.post(
        "/api/config/routing/entry",
        json={"entry": bad, "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "validation"


def test_routing_post_conflict_409(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.post(
        "/api/config/routing/entry",
        json={"entry": _routing_entry(), "base_hash": "sha256:stale"},
    )
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["code"] == "conflict"


def test_routing_put_update_entry(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.put(
        "/api/config/routing/entry/0",
        json={"changes": {"model": "qwen2.5-coder-32b"}, "base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] == "staged_next_job"
    on_disk = yaml.safe_load(routing_path.read_text())["entries"]
    assert on_disk[0]["model"] == "qwen2.5-coder-32b"


def test_routing_delete_entry(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.request(
        "DELETE",
        "/api/config/routing/entry/0",
        json={"base_hash": _routing_hash(client)},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert yaml.safe_load(routing_path.read_text())["entries"] == []


def test_routing_write_unavailable_403(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.post(
        "/api/config/routing/entry",
        json={"entry": _routing_entry(), "base_hash": "sha256:x"},
    )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "forbidden"


# --- Tolerant IO / structured-error hardening (Copilot round 13) --------------
#
# The API-handler layer must mirror the service layer's tolerant-read /
# structured-error guarantees: a malformed on-disk config must not 500 the
# display fetch, a non-object JSON body must yield a structured 422 (not an
# AttributeError 500), and the routing read-only diagnostic must stay accurate
# when an endpoint mounts a routing path whose host file is missing.


def test_config_all_degrades_on_malformed_yaml(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    # Corrupt the on-disk file out-of-band (mount glitch / concurrent edit).
    temp_config_path.write_text("global: [unclosed\n  bad: : :\n")
    resp = client.get("/api/config/all")
    # The descriptor layer can still render from the in-memory validated config;
    # only the raw "${VAR}" display values degrade. No unstructured 500.
    assert resp.status_code == 200
    assert {s["id"] for s in resp.json()["sections"]} == SECTION_IDS


def test_config_section_degrades_on_malformed_yaml(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    temp_config_path.write_text("global: [unclosed\n  bad: : :\n")
    resp = client.get("/api/config/section/global")
    assert resp.status_code == 200
    assert resp.json()["id"] == "global"


def test_config_all_degrades_on_invalid_utf8(temp_config_path):
    """An out-of-band edit can leave config.yaml with invalid UTF-8 bytes.

    The display fetch reads the raw file to surface ``${VAR}`` literals; reading
    invalid-encoding bytes raises ``UnicodeDecodeError``. The tolerant-read path
    must catch that (alongside OSError/YAMLError) and degrade the raw display
    values rather than 500 the whole endpoint — the descriptor layer still renders
    from the in-memory validated config.
    """
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    # Invalid UTF-8 continuation bytes (mount glitch / corrupt write).
    temp_config_path.write_bytes(b"\xff\xfe\x00bad bytes not utf-8\xc3\x28")
    resp = client.get("/api/config/all")
    assert resp.status_code == 200
    assert {s["id"] for s in resp.json()["sections"]} == SECTION_IDS


def test_put_section_non_object_body_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.put("/api/config/section/global", json=[])
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "validation"
    assert "Traceback" not in body["errors"][0]["message"]


def test_put_section_bad_changes_type_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    base_hash = _config_yaml_hash(client)
    # `changes` must be a mapping; a string trips pydantic ValidationError on
    # SaveRequest, which must surface as a structured 422 (not a 500).
    resp = client.put(
        "/api/config/section/global",
        json={"changes": "not-a-dict", "base_hash": base_hash},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "validation"
    assert "Traceback" not in body["errors"][0]["message"]


def test_post_catalog_non_object_body_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.post("/api/config/catalog/endpoints", json=[])
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "validation"


def test_put_catalog_non_object_body_422(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.put("/api/config/catalog/endpoints/openai-native", json=[])
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "validation"


def test_post_routing_non_object_body_422(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    raw = yaml.safe_load(config_path.read_text())
    client = _make_client(config_path, raw)
    resp = client.post("/api/config/routing/entry", json=[])
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "validation"


def test_routing_write_mounted_but_missing_specific_message(temp_config_path):
    # REPRESENTATIVE_CONFIG mounts routing at a host path that does not exist.
    # The read-only error must name the mounting endpoint and say the file is
    # missing — not the generic "no endpoint mounts a routing table".
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.post(
        "/api/config/routing/entry",
        json={"entry": _routing_entry(), "base_hash": "sha256:x"},
    )
    assert resp.status_code == 403
    msg = resp.json()["errors"][0]["message"]
    assert "vllm-box" in msg
    assert "not a file" in msg


# --- T049: pre-existing endpoint contract regression (FR-018) -----------------
#
# The 081 config UI must EXTEND, never break, the endpoints that shipped before
# it (018 personas, Task 9/10 effective/reload, the original /api/config/global
# slice). These tests pin those contracts so a future 081 refactor can't silently
# alter them.

_LEGACY_GLOBAL_FIELDS = {
    "poll_interval_seconds",
    "heartbeat_interval_seconds",
    "max_concurrent_cards",
    "max_feedback_cycles",
    "max_closed_pr_attempts_per_issue",
    "log_level",
    "output_mode",
    "assignee_filter",
    "human_reviewers",
    "trusted_bot_reviewers",
    "env_cache_root",
}


def test_legacy_get_config_global_returns_exact_field_slice(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/global")
    assert resp.status_code == 200
    # The exact 11-field editable slice — no more, no less.
    assert set(resp.json()) == _LEGACY_GLOBAL_FIELDS


def test_legacy_put_config_global_persists_and_reports_saved(temp_config_path, monkeypatch):
    # This legacy endpoint fully re-validates ProjectConfiguration, which rejects
    # the fixture's ${COORDINARE_GITHUB_TOKEN} placeholder unless it is resolvable.
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_realtoken")
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    etag = client.get("/api/config/global").headers["etag"]  # 157: version required
    resp = client.put(
        "/api/config/global", json={"poll_interval_seconds": 33, "expected_hash": etag},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"
    assert yaml.safe_load(temp_config_path.read_text())["poll_interval_seconds"] == 33


def test_legacy_put_config_global_rejects_unknown_field_400(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    etag = client.get("/api/config/global").headers["etag"]  # 157: version required
    resp = client.put("/api/config/global", json={"not_a_field": 1, "expected_hash": etag})
    assert resp.status_code == 400


def test_legacy_get_config_effective_default_mode(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/config/effective")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"github_org", "github_project_number", "project_name", "mode"}


def test_legacy_post_config_reload_accepted(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.post("/api/config/reload")
    # MagicMock daemon exposes _config_reload_trigger → reload is triggered (202).
    assert resp.status_code == 202
    assert resp.json()["status"] == "reload_triggered"


def test_legacy_personas_list_returns_all_roles(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.get("/api/personas")
    assert resp.status_code == 200
    body = resp.json()
    assert {p["role"] for p in body} == {
        "advocate", "architect", "assessor", "closer", "curator", "implementer",
        "qa", "reviewer", "security", "tech_writer",
    }
    for p in body:
        assert set(p) == {"role", "instructions", "is_default"}


def test_legacy_persona_put_then_delete_round_trip(temp_config_path, monkeypatch):
    # The PUT re-reads the config via load_personas_hot to echo the effective
    # value; that re-read validates ProjectConfiguration, so the ${VAR} token
    # placeholder must resolve.
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_realtoken")
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    put = client.put(
        "/api/personas/reviewer",
        json={"instructions": "Be thorough."},
        headers=_version_header(temp_config_path),
    )
    assert put.status_code == 200
    body = put.json()
    assert body["role"] == "reviewer"
    assert body["is_default"] is False
    assert "Be thorough." in body["instructions"]

    delete = client.delete(
        "/api/personas/reviewer", headers=_version_header(temp_config_path),
    )
    assert delete.status_code == 204


def test_legacy_persona_put_unknown_role_404(temp_config_path):
    raw = yaml.safe_load(temp_config_path.read_text())
    client = _make_client(temp_config_path, raw)
    resp = client.put("/api/personas/not_a_role", json={"instructions": "x"})
    assert resp.status_code == 404
