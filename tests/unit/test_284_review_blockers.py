from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinare.config import ProjectConfiguration
from coordinare.config_descriptors import SECRET_MASK
from coordinare.services import config_seed
from coordinare.services.config_secret_policy import persistent_secret_problem
from coordinare.services.config_write_service import _validate_candidate, atomic_write_yaml
from tests.unit.test_147_helm_deployment import _one, _render
from tests.unit.test_221_symphony_editor import _client
from tests.unit.test_284_persistent_config import SEED, _dashboard, _paths

GARDEN = Path(__file__).resolve().parents[1] / "fixtures/284/garden-config.yaml"
OLD_PORT = 8090
NEW_PORT = 8098
USER_INTERVAL = 77


def test_exact_rendered_garden_seed(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    seed.write_bytes(GARDEN.read_bytes())
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text()) == yaml.safe_load(seed.read_text())


@pytest.mark.parametrize("key", [
    "CODEX_PROVIDER_ENV_KEY", "AUTH_ENV", "SECRET_VAR", "TOKEN_NAME",
    "codex_provider_env_key", "auth_env", "secret_var", "token_name",
])
def test_secret_pointer_names_are_allowed_but_literal_secrets_are_not(key: str) -> None:
    raw = {"performer_endpoints": [{"env": {key: "LITELLM_MASTER_KEY"}}]}
    assert persistent_secret_problem(raw) is None
    raw["performer_endpoints"][0]["env"][key] = "sk-real-secret"
    assert persistent_secret_problem(raw) is not None
    assert persistent_secret_problem(
        {"performer_endpoints": [{"env": {"API_KEY": "LITERAL_SECRET"}}]},
    )
    assert persistent_secret_problem({"github_token": "LITERAL_SECRET"})


@pytest.mark.parametrize("key", ["MY_TOKEN_ENV_KEY", "MY_TOKEN_ENV", "MY_TOKEN_VAR", "MY_TOKEN_NAME"])
@pytest.mark.parametrize("value", ["HUNTER2", "GHPTOKEN0123ABCD"])
def test_uppercase_literals_are_not_secret_pointer_names(key: str, value: str) -> None:
    assert persistent_secret_problem({"performer_endpoints": [{"env": {key: value}}]})


def test_boot_overlays_chart_fields_and_preserves_user_fields(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    raw = {
        **yaml.safe_load(SEED),
        "dashboard_port": OLD_PORT,
        "performer_image": "performer:old",
        "kubernetes_cache_claim": "old-claim",
        "observability": {"metrics_enabled": False},
    }
    seed.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    edited = {**raw, "poll_interval_seconds": USER_INTERVAL}
    live.write_text(yaml.safe_dump(edited))
    upgraded = {
        **raw,
        "dashboard_port": NEW_PORT,
        "performer_image": "performer:new",
        "observability": {"metrics_enabled": True},
    }
    upgraded.pop("kubernetes_cache_claim")
    seed.write_text(yaml.safe_dump(upgraded))
    with patch("coordinare.services.config_seed._log") as log:
        config_seed.seed_config(seed, live)
    actual = yaml.safe_load(live.read_text())
    assert actual == {**upgraded, "poll_interval_seconds": USER_INTERVAL}
    assert (
        ProjectConfiguration(
            **{**actual, "github_token": "fixture", "human_reviewers": ["alice"]},
        ).dashboard_port
        == NEW_PORT
    )
    assert log.info.call_count >= len(
        upgraded.keys() & {"dashboard_port", "performer_image", "observability"},
    )
    assert config_seed.seed_status(seed, live)["chart_changed"] is False


@pytest.mark.parametrize("content", ["", " \n\t ", "github_org: ["])
def test_bad_live_file_is_preserved_and_reseeded(tmp_path: Path, content: str) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(content)
    with patch("coordinare.services.config_seed._log") as log:
        config_seed.seed_config(seed, live)
    backups = list(live.parent.glob("config.yaml.broken-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == content
    assert yaml.safe_load(live.read_text()) == yaml.safe_load(SEED)
    log.error.assert_called_once()
    assert config_seed.seed_status(seed, live)["recovery"]["backup"] == backups[0].name
    config_seed.seed_config(seed, live)
    assert len(list(live.parent.glob("config.yaml.broken-*"))) == 1


@pytest.mark.parametrize("content", ["[]", "literal-scalar", "42", "null", "# comment only\n", "1: value"])
def test_parseable_nonmapping_live_file_is_refused_intact(tmp_path: Path, content: str) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    metadata = (live.parent / "seed.yaml").read_bytes()
    live.write_text(content)
    with pytest.raises(ValueError, match="YAML mapping with string keys"):
        config_seed.seed_config(seed, live)
    assert live.read_text() == content
    assert (live.parent / "seed.yaml").read_bytes() == metadata
    assert not list(live.parent.glob("config.yaml.broken-*"))
    assert not (live.parent / "recovery.yaml").exists()


def test_recovery_marker_failure_keeps_broken_live_file(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text("invalid: [")
    with (
        patch.object(config_seed, "atomic_write_yaml", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        config_seed.seed_config(seed, live)
    assert live.read_text() == "invalid: ["
    assert not list(live.parent.glob("config.yaml.broken-*"))


def test_recovery_notice_survives_failure_after_rename(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text("invalid: [")
    original_write = config_seed.atomic_write_yaml

    def fail_reseed(path: Path, raw: dict) -> str:
        if path == live:
            raise OSError("disk full")
        return original_write(path, raw)

    with (
        patch.object(config_seed, "atomic_write_yaml", side_effect=fail_reseed),
        pytest.raises(OSError, match="disk full"),
    ):
        config_seed.seed_config(seed, live)
    recovery = config_seed.recovery_status(live)
    assert recovery is not None
    assert (live.parent / recovery["backup"]).read_text() == "invalid: ["


def test_parseable_validation_failure_is_never_replaced(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    invalid = "github_org: acme\ngithub_token: null\npoll_interval_seconds: -1\n"
    live.write_text(invalid)
    config_seed.seed_config(seed, live)
    assert live.read_text() == invalid
    assert not list(live.parent.glob("config.yaml.broken-*"))


def test_comment_and_chart_owned_edits_do_not_report_drift(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    seed.write_text("# revised comment\n" + SEED + f"dashboard_port: {NEW_PORT}\n")
    assert config_seed.seed_status(seed, live)["chart_changed"] is False
    assert config_seed.seed_status(seed, live)["changed_sections"] == []
    seed.write_text(seed.read_text().replace("acme", "changed"))
    assert config_seed.seed_status(seed, live)["changed_sections"] == ["github_org"]
    assert config_seed.seed_status(seed, live)["chart_changed"] is True


def test_chart_fields_are_readonly_and_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, live, client, _ = _dashboard(tmp_path, monkeypatch)
    snapshot = client.get("/api/config/all").json()
    port = next(
        s
        for section in snapshot["sections"]
        for s in section["settings"]
        if s["key"] == "global.dashboard_port"
    )
    assert port["editable"] is False
    assert "managed by the Helm chart" in port["help"]
    before = live.read_bytes()
    response = client.put(
        "/api/config/section/global",
        json={
            "base_hash": snapshot["content_hashes"]["config_yaml"],
            "changes": {"dashboard_port": NEW_PORT},
        },
    )
    assert response.status_code == 403
    assert live.read_bytes() == before
    assert (
        client.put(
            "/api/config/global",
            headers={"If-Match": snapshot["content_hashes"]["config_yaml"]},
            json={"env_cache_root": "/tmp/changed-cache"},
        ).status_code
        == 403
    )


def test_recovery_is_visible_in_system_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, daemon = _dashboard(tmp_path, monkeypatch)
    live.write_text("broken: [")
    config_seed.seed_config(seed, live)
    daemon.state["config_path"] = live
    response = client.get("/api/state")
    assert response.status_code == 200
    row = next(
        row for row in response.json()["system_health"]["rows"] if row["key"] == "config_recovery"
    )
    assert row["status"] == "degraded"
    assert response.json()["system_health"]["overall"] == "degraded"
    assert "config.yaml.broken-" in row["reason"]


def test_config_checksum_depends_on_seed_data_only() -> None:
    values = {"fullnameOverride": "fixed", "config.kubernetes_owner": "fixed-owner"}
    first = _render(**{**values, "config.performer_image": "performer:old"})
    second = _render(**{**values, "config.performer_image": "performer:new"})

    def checksum(objects: list[dict]) -> str:
        return _one(objects, "StatefulSet")["spec"]["template"]["metadata"]["annotations"][
            "checksum/config"
        ]

    assert checksum(first) != checksum(second)
    assert checksum(first) == checksum(
        _render(release="another", **{**values, "config.performer_image": "performer:old"}),
    )


def test_chart_adoption_masks_diff_and_selectively_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, daemon = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(seed.read_text())
    raw.update(
        github_org="new-org",
        poll_interval_seconds=USER_INTERVAL,
        performer_endpoints=[{"name": "test", "env": {"API_KEY": "${GITHUB_TOKEN}"}}],
    )
    seed.write_text(yaml.safe_dump(raw))
    preview = client.get("/api/config/chart")
    assert preview.status_code == 200, preview.text
    assert "ghp_runtime_secret" not in preview.text
    assert "${GITHUB_TOKEN}" not in preview.text
    assert SECRET_MASK in preview.text
    body = {"sections": ["github_org"], "confirmed": True, "seed_hash": preview.json()["seed_hash"]}
    headers = {"If-Match": preview.headers["ETag"]}
    before = live.read_bytes()
    assert (
        client.post(
            "/api/config/chart/adopt", json={**body, "confirmed": False}, headers=headers,
        ).status_code
        == 422
    )
    assert client.post("/api/config/chart/adopt", json=body).status_code == 428
    assert live.read_bytes() == before
    response = client.post("/api/config/chart/adopt", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert yaml.safe_load(live.read_text())["github_org"] == "new-org"
    assert "poll_interval_seconds" not in yaml.safe_load(live.read_text())
    assert daemon._config_reload_trigger.is_set()
    assert config_seed.seed_status(seed, live)["changed_sections"] == [
        "performer_endpoints",
        "poll_interval_seconds",
    ]
    assert client.post("/api/config/chart/adopt", json=body, headers=headers).status_code == 409
    assert (
        client.post(
            "/api/config/chart/adopt",
            json=body,
            headers={**headers, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert "new-org" not in json.dumps(config_seed.seed_status(seed, live))


def test_adoption_refuses_a_changed_seed_and_invalid_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    preview = client.get("/api/config/chart")
    assert preview.status_code == 200, preview.text
    body = {
        "sections": ["poll_interval_seconds"],
        "confirmed": True,
        "seed_hash": preview.json()["seed_hash"],
    }
    headers = {"If-Match": preview.headers["ETag"]}
    seed.write_text(seed.read_text() + "poll_interval_seconds: -1\n")
    before = live.read_bytes()
    assert client.post("/api/config/chart/adopt", json=body, headers=headers).status_code == 409
    body["seed_hash"] = client.get("/api/config/chart").json()["seed_hash"]
    assert client.post("/api/config/chart/adopt", json=body, headers=headers).status_code == 422
    assert live.read_bytes() == before


def test_garden_endpoint_image_pins_follow_chart_by_id(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    original = yaml.safe_load(GARDEN.read_text())
    seed.write_text(yaml.safe_dump(original))
    config_seed.seed_config(seed, live)
    edited = yaml.safe_load(live.read_text())
    edited["performer_endpoints"].reverse()
    edited["performer_endpoints"][0]["roles"] = ["qa"]
    live.write_text(yaml.safe_dump(edited))
    for endpoint in original["performer_endpoints"]:
        endpoint["image"] = "performer:renovated"
    seed.write_text(yaml.safe_dump(original))
    config_seed.seed_config(seed, live)
    actual = yaml.safe_load(live.read_text())
    assert all(
        endpoint["image"] == "performer:renovated" for endpoint in actual["performer_endpoints"]
    )
    assert actual["performer_endpoints"][0]["roles"] == ["qa"]
    assert config_seed.seed_status(seed, live)["chart_changed"] is False


def test_adoption_rechecks_live_and_seed_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    seed.write_text(seed.read_text().replace("acme", "new-org"))
    preview = client.get("/api/config/chart")
    body = {"sections": ["github_org"], "confirmed": True, "seed_hash": preview.json()["seed_hash"]}
    headers = {"If-Match": preview.headers["ETag"]}
    before = live.read_bytes()

    def concurrent_chart(_candidate: dict, *, original: dict | None = None) -> list:
        seed.write_text(seed.read_text().replace("new-org", "newer-org"))
        return []

    with patch(
        "coordinare.services.config_adoption._validate_candidate", side_effect=concurrent_chart,
    ):
        assert client.post("/api/config/chart/adopt", json=body, headers=headers).status_code == 409
    assert live.read_bytes() == before
    body["seed_hash"] = client.get("/api/config/chart").json()["seed_hash"]

    def concurrent_live(_candidate: dict, *, original: dict | None = None) -> list:
        live.write_text(live.read_text() + "\npoll_interval_seconds: 75\n")
        return []

    with patch(
        "coordinare.services.config_adoption._validate_candidate", side_effect=concurrent_live,
    ):
        assert client.post("/api/config/chart/adopt", json=body, headers=headers).status_code == 409
    assert yaml.safe_load(live.read_text())["github_org"] == "acme"


def test_preview_masks_nested_symphony_and_workflow_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _, client, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(seed.read_text())
    raw["symphonies"][0]["overrides"]["webhooks"] = {"secret": "nested-private-value"}
    raw["performers"] = {"implementer": {"workflow_env": {"API_KEY": "workflow-private-value"}}}
    seed.write_text(yaml.safe_dump(raw))
    response = client.get("/api/config/chart")
    assert response.status_code == 200
    assert "nested-private-value" not in response.text
    assert "workflow-private-value" not in response.text
    assert SECRET_MASK in response.text


def test_chart_fields_and_endpoint_pins_are_displayed_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, _, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw.update(
        performer_image="performer:pin",
        performer_endpoints=[
            {"id": "demo", "image": "performer:pin", "roles": ["implementer"], "mode": "ephemeral"},
        ],
        observability={"metrics_enabled": False},
    )
    live.write_text(yaml.safe_dump(raw))
    seed.write_text(yaml.safe_dump(raw))
    client, _ = _client(live, raw)
    settings = client.get("/api/config/all").json()["sections"][0]["settings"]
    for key in [
        "global.observability",
        "global.performer_image",
        "global.performer_endpoints.demo.image",
    ]:
        field = next(field for field in settings if field["key"] == key)
        assert field["editable"] is False
        assert field["help"] == "managed by the Helm chart"


def test_start_check_failure_keeps_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, _, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw["github_auth"] = "app"
    raw["github_app_id"] = None
    live.write_text(yaml.safe_dump(raw))
    before = live.read_bytes()
    assert _validate_candidate(raw)
    config_seed.seed_config(seed, live)
    assert live.read_bytes() == before
    assert not list(live.parent.glob("config.yaml.broken-*"))


def test_adoption_acknowledges_the_seed_written_even_if_configmap_rotates_after_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    seed.write_text(seed.read_text().replace("acme", "new-org"))
    preview = client.get("/api/config/chart")
    body = {"sections": ["github_org"], "confirmed": True, "seed_hash": preview.json()["seed_hash"]}
    headers = {"If-Match": preview.headers["ETag"]}
    original_write = atomic_write_yaml

    def rotate_after_write(path: Path, raw: dict) -> str:
        version = original_write(path, raw)
        seed.write_text(seed.read_text().replace("new-org", "newer-org"))
        return version

    with patch(
        "coordinare.services.config_adoption.atomic_write_yaml", side_effect=rotate_after_write,
    ):
        response = client.post("/api/config/chart/adopt", json=body, headers=headers)
    assert response.status_code == 200
    assert yaml.safe_load(live.read_text())["github_org"] == "new-org"
    assert config_seed.seed_status(seed, live)["chart_changed"] is True
    assert config_seed.seed_status(seed, live)["changed_sections"] == ["github_org"]


def test_user_endpoint_image_without_chart_pin_survives_and_is_reported(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    raw = yaml.safe_load(SEED)
    raw["performer_endpoints"] = [{"id": "demo", "roles": ["qa"], "mode": "ephemeral"}]
    seed.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    raw["performer_endpoints"][0]["image"] = "user:own-image"
    live.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text())["performer_endpoints"][0]["image"] == "user:own-image"
    assert config_seed.seed_status(seed, live)["live_diff_sections"] == ["performer_endpoints"]


def test_chart_endpoint_image_cannot_be_removed_from_existing_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, _, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(seed.read_text())
    raw["performer_endpoints"] = [
        {"id": "demo", "roles": ["qa"], "mode": "ephemeral", "image": "chart:pin"},
    ]
    seed.write_text(yaml.safe_dump(raw))
    raw["performer_endpoints"][0].pop("image")
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].code == "forbidden"
    assert "managed by the Helm chart" in errors[0].message
    assert live.is_file()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("header", ["Authorization", "x-api-key", "x-tenant"])
def test_otlp_headers_require_placeholders(tmp_path: Path, nested: bool, header: str) -> None:
    seed, live = _paths(tmp_path)
    document = {"observability": {"otlp": {"headers": {header: "Bearer literal-abc"}}}}
    raw = {"symphonies": [{"name": "demo", "overrides": document}]} if nested else document
    seed.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="placeholder") as error:
        config_seed.seed_config(seed, live)
    assert "literal-abc" not in str(error.value)
    assert not live.exists()
    document["observability"]["otlp"]["headers"][header] = "${OTLP_AUTH}"
    seed.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    assert "${OTLP_AUTH}" in live.read_text()


def test_preview_masks_otlp_headers_and_url_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    for path in (seed, live):
        raw = yaml.safe_load(path.read_text())
        raw["symphonies"][0]["overrides"].update(
            observability={"otlp": {"headers": {"Authorization": "Bearer literal-abc"}}},
            performer_endpoints=[{"base_url": "postgres://u:literal-password@h"}],
        )
        if path == seed:
            raw["symphonies"][0]["overrides"]["max_concurrent_cards"] = 9
        path.write_text(yaml.safe_dump(raw))
    response = client.get("/api/config/chart")
    assert response.status_code == 200
    assert "literal-abc" not in response.text
    assert "literal-password" not in response.text
    assert SECRET_MASK in response.text


@pytest.mark.parametrize("url", [
    "postgres://u:pw@h", "https://u:pw@h/path", "postgres://u@h", "postgres://u:@h",
])
@pytest.mark.parametrize("location", ["endpoint", "env", "override"])
def test_url_userinfo_requires_a_password_placeholder(
    tmp_path: Path, url: str, location: str,
) -> None:
    seed, live = _paths(tmp_path)
    if location == "endpoint":
        raw = {"endpoints": [{"base_url": url}]}
    elif location == "env":
        raw = {"performer_endpoints": [{"env": {"DATABASE_URL": url}}]}
    else:
        raw = {"symphonies": [{"overrides": {"proxy_url": url}}]}
    seed.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="placeholder"):
        config_seed.seed_config(seed, live)
    assert not live.exists()
    seed.write_text(seed.read_text().replace(url, "postgres://u:${DATABASE_PASSWORD}@h"))
    config_seed.seed_config(seed, live)
    assert "${DATABASE_PASSWORD}" in live.read_text()


def test_unpinned_private_key_path_survives_boot_and_remains_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw["github_private_key_path"] = "/keys/operator.pem"
    live.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text())["github_private_key_path"] == "/keys/operator.pem"
    assert "github_private_key_path" in config_seed.seed_status(seed, live)["live_diff_sections"]
    response = client.put(
        "/api/config/section/global",
        json={"base_hash": config_seed._read_document(live)[1],
              "changes": {"github_private_key_path": "/keys/new.pem"}},
    )
    assert response.status_code == 200, response.text
    raw = yaml.safe_load(seed.read_text())
    raw["github_private_key_path"] = "/keys/chart.pem"
    seed.write_text(yaml.safe_dump(raw))
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text())["github_private_key_path"] == "/keys/chart.pem"
    response = client.put(
        "/api/config/section/global",
        json={"base_hash": config_seed._read_document(live)[1],
              "changes": {"github_private_key_path": "/keys/forbidden.pem"}},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("route", ["adopt", "section", "global", "symphony", "catalog"])
def test_unrelated_writes_allow_pending_chart_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw["dashboard_port"] = OLD_PORT
    raw["performer_endpoints"] = [
        {"id": "demo", "roles": ["qa"], "mode": "ephemeral", "image": "chart:old"},
    ]
    live.write_text(yaml.safe_dump(raw))
    upgraded = {**raw, "dashboard_port": NEW_PORT, "github_org": "new-org",
                "performer_endpoints": [{**raw["performer_endpoints"][0], "image": "chart:new"}]}
    seed.write_text(yaml.safe_dump(upgraded))
    version = config_seed._read_document(live)[1]
    if route == "adopt":
        preview = client.get("/api/config/chart").json()
        response = client.post(
            "/api/config/chart/adopt", headers={"If-Match": version},
            json={"sections": ["github_org"], "confirmed": True, "seed_hash": preview["seed_hash"]},
        )
    elif route == "section":
        response = client.put("/api/config/section/global", json={
            "base_hash": version, "changes": {"poll_interval_seconds": USER_INTERVAL},
        })
    elif route == "global":
        response = client.put("/api/config/global", headers={"If-Match": version},
                              json={"poll_interval_seconds": USER_INTERVAL})
    elif route == "symphony":
        response = client.put("/api/symphonies/demo", headers={"If-Match": version},
                              json={"overrides": {"max_concurrent_cards": 5}})
    else:
        response = client.post("/api/config/catalog/endpoints", json={
            "base_hash": version, "item": {"name": "new", "kind": "vllm", "base_url": "http://h"},
        })
    assert response.status_code == 200, response.text
    # An explicit edit to a chart-owned field must still fail before boot overlays it.
    response = client.put("/api/config/section/global", json={
        "base_hash": config_seed._read_document(live)[1], "changes": {"dashboard_port": 9001},
    })
    assert response.status_code == 403
    assert "managed by the Helm chart" in response.text
    original = yaml.safe_load(live.read_text())
    changed = {**original, "performer_endpoints": [
        {**original["performer_endpoints"][0], "image": "user:forbidden"},
    ]}
    errors = _validate_candidate(changed, original=original)
    assert errors and errors[0].code == "forbidden"
    assert "performer_endpoints.demo.image" in errors[0].message


def test_partial_adoption_then_chart_revert_still_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    original = seed.read_bytes()
    raw = yaml.safe_load(original)
    seed.write_text(yaml.safe_dump({**raw, "github_org": "new-org", "poll_interval_seconds": 75}))
    preview = client.get("/api/config/chart")
    response = client.post(
        "/api/config/chart/adopt", headers={"If-Match": preview.headers["ETag"]},
        json={"sections": ["github_org"], "confirmed": True, "seed_hash": preview.json()["seed_hash"]},
    )
    assert response.status_code == 200, response.text
    seed.write_bytes(original)
    status = config_seed.seed_status(seed, live)
    assert status["chart_changed"] is True
    assert status["changed_sections"] == ["github_org"]


def test_adoption_runs_outside_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.dashboard.routers import config_chart

    seed, _, client, _ = _dashboard(tmp_path, monkeypatch)
    seed.write_text(seed.read_text().replace("acme", "new-org"))
    preview = client.get("/api/config/chart")
    original_adopt = config_chart.adopt_chart_sections
    original_paths = config_chart._paths
    request_threads = []
    worker_threads = []

    def request_paths(ctx):
        request_threads.append(threading.get_ident())
        return original_paths(ctx)

    def worker_adopt(*args):
        worker_threads.append(threading.get_ident())
        return original_adopt(*args)

    with (
        patch.object(config_chart, "_paths", side_effect=request_paths),
        patch.object(config_chart, "adopt_chart_sections", side_effect=worker_adopt),
    ):
        response = client.post(
            "/api/config/chart/adopt", headers={"If-Match": preview.headers["ETag"]},
            json={"sections": ["github_org"], "confirmed": True,
                  "seed_hash": preview.json()["seed_hash"]},
        )
    assert response.status_code == 200, response.text
    assert worker_threads and request_threads
    assert worker_threads[0] != request_threads[0]


def test_chart_overlay_refuses_literal_headers_before_writing(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    before = live.read_bytes()
    raw = yaml.safe_load(seed.read_text())
    raw["observability"] = {"otlp": {"headers": {"Authorization": "Bearer literal-abc"}}}
    seed.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="placeholder"):
        config_seed.seed_config(seed, live)
    assert live.read_bytes() == before


@pytest.mark.parametrize("content", [b"\xff\xfe\xff", b"\x80invalid"])
def test_undecodable_live_file_is_preserved_and_reseeded(tmp_path: Path, content: bytes) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_bytes(content)
    config_seed.seed_config(seed, live)
    backup = next(live.parent.glob("config.yaml.broken-*"))
    assert backup.read_bytes() == content
    assert yaml.safe_load(live.read_text()) == yaml.safe_load(seed.read_text())


def test_string_form_otlp_headers_cannot_bypass_the_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(seed.read_text())
    raw["symphonies"][0]["overrides"]["observability"] = {
        "otlp": {"headers": "Authorization=Bearer literal-abc"},
    }
    seed.write_text(yaml.safe_dump(raw))
    assert persistent_secret_problem(raw)
    preview = client.get("/api/config/chart")
    assert preview.status_code == 200
    assert "literal-abc" not in preview.text
    assert SECRET_MASK in preview.text
    raw["symphonies"][0]["overrides"]["observability"]["otlp"]["headers"] = "${OTLP_HEADERS}"
    assert persistent_secret_problem(raw) is None
    assert live.is_file()
