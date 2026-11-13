"""Spec 557: endpoint-scoped routing ConfigMap projection, without a cluster."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from coordinare.config_validation import validate_config
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.kubernetes_runtime import KubernetesRuntime, build_pod_manifest

ROUTING_PATH = "/etc/coordinare/routing/routing.yaml"


def _endpoint(**overrides) -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate({
        "id": "claude", "mode": "ephemeral", "roles": ["assessor"],
        "image": "performer:full", **overrides,
    })


def _manifest(config, **kwargs):
    return build_pod_manifest(config, pod_name="claude-1", performer_id=config.id, **kwargs)


@pytest.mark.parametrize("name", [
    "", "BadName", "bad_name", "bad/name", "bad name", "-bad", "bad-",
    ".bad", "bad.", "bad..name", "bad.-name", "a" * 254, "${UNSET}", "bad\n",
])
def test_rejects_invalid_configmap_names(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        _endpoint(kubernetes_routing_configmap={"name": name})


@pytest.mark.parametrize("name", ["routing", "samplehost-routing-v1", "samplehost.routing", "9", "a" * 253])
def test_accepts_dns1123_subdomain_names(name: str) -> None:
    config = _endpoint(kubernetes_routing_configmap={"name": name})
    assert config.kubernetes_routing_configmap.name == name
    assert config.kubernetes_routing_configmap.key == "routing.yaml"


@pytest.mark.parametrize("key", ["", ".", "..", "..routes", "../routing.yaml", "a/b", "bad key", "x" * 254])
def test_rejects_invalid_configmap_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="key"):
        _endpoint(kubernetes_routing_configmap={"name": "routing", "key": key})


def test_subprocess_endpoint_rejects_routing_mount() -> None:
    with pytest.raises(ValidationError, match=r"subprocess performers must not set.*kubernetes_routing_configmap"):
        PerformerEndpointConfig(
            id="local", roles=["assessor"],
            kubernetes_routing_configmap={"name": "routing"},
        )


@pytest.mark.parametrize("env", [{}, {"SELFHOSTED_ROUTING_CONFIG": "/baked/routing.yaml", "OTHER": "yes"}])
@pytest.mark.parametrize("overrides", [{}, {"kubernetes_routing_configmap": None}])
def test_default_leaves_mounts_and_environment_unchanged(env, overrides) -> None:
    config = _endpoint(**overrides)
    assert config.kubernetes_routing_configmap is None
    spec = _manifest(config, env=env)["spec"]
    container = spec["containers"][0]
    assert "volumes" not in spec
    assert "volumeMounts" not in container
    assert {e["name"]: e["value"] for e in container.get("env", [])} == env


@pytest.mark.parametrize("name, passed", [("samplehost-routing", True), ("Bad_Name", False)])
def test_config_validator_checks_endpoint_routing_from_yaml(tmp_path, monkeypatch, name, passed) -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    raw = yaml.safe_load(example.read_text())
    raw["performer_endpoints"] = [{
        "id": "claude", "mode": "ephemeral", "roles": ["assessor"], "image": "performer:full",
        "kubernetes_routing_configmap": {"name": name},
    }]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_placeholder")
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    result = validate_config(config_path)
    assert result.passed is passed, result.errors
    if not passed:
        assert any("kubernetes_routing_configmap.name" in e.field_path for e in result.errors)


@pytest.mark.parametrize("key", ["routing.yaml", "custom-routes.yaml"])
def test_projects_one_key_read_only_and_sets_env_without_mutating_input(key: str) -> None:
    config = _endpoint(kubernetes_routing_configmap={"name": "samplehost-routing", "key": key})
    env = {"SELFHOSTED_ROUTING_CONFIG": "/wrong/path", "KEEP": "value"}
    spec = _manifest(config, env=env)["spec"]
    container = spec["containers"][0]
    assert spec["volumes"] == [{
        "name": "selfhosted-routing",
        "configMap": {"name": "samplehost-routing", "items": [{"key": key, "path": "routing.yaml"}]},
    }]
    assert container["volumeMounts"] == [{
        "name": "selfhosted-routing", "mountPath": "/etc/coordinare/routing", "readOnly": True,
    }]
    assert {e["name"]: e["value"] for e in container["env"]} == {
        "SELFHOSTED_ROUTING_CONFIG": ROUTING_PATH, "KEEP": "value",
    }
    assert env["SELFHOSTED_ROUTING_CONFIG"] == "/wrong/path"
    assert spec["automountServiceAccountToken"] is False


def test_mount_coexists_with_cache_and_does_not_leak_to_other_endpoint() -> None:
    config = _endpoint(
        kubernetes_routing_configmap={"name": "samplehost-routing"},
        volumes=[{"host_path": "/cache/samplehost", "container_path": "/devenv/samplehost", "mode": "ro"}],
    )
    spec = _manifest(config, cache_claim="cache")["spec"]
    mounts = spec["containers"][0]["volumeMounts"]
    assert {m["name"] for m in mounts} == {"devenv-cache", "selfhosted-routing"}
    assert mounts[0] == {
        "name": "devenv-cache", "mountPath": "/devenv/samplehost", "subPath": "samplehost", "readOnly": True,
    }
    assert spec["volumes"][0] == {"name": "devenv-cache", "persistentVolumeClaim": {"claimName": "cache"}}
    assert "volumes" not in _manifest(_endpoint(id="other"))["spec"]
    assert "env" not in _manifest(_endpoint(id="other"))["spec"]["containers"][0]


def test_routing_sets_environment_even_without_other_env() -> None:
    spec = _manifest(_endpoint(kubernetes_routing_configmap={"name": "routing"}))["spec"]
    assert spec["containers"][0]["env"] == [{"name": "SELFHOSTED_ROUTING_CONFIG", "value": ROUTING_PATH}]


@pytest.mark.asyncio
async def test_dispatch_submits_projection_with_no_configmap_api_calls() -> None:
    api = Mock(spec=["create_namespaced_pod", "read_namespaced_pod"])
    api.read_namespaced_pod.return_value = SimpleNamespace(status=SimpleNamespace(
        phase="Running", pod_ip="10.0.0.2", reason="", container_statuses=[],
    ))
    config = _endpoint(
        kubernetes_routing_configmap={"name": "samplehost-routing"},
        env={"OTHER": "value", "SELFHOSTED_ROUTING_CONFIG": "/old"},
    )
    await KubernetesRuntime(namespace="samplehost-performers", core_v1=api).start_ephemeral(
        config, backend="claude_code",
    )
    api.create_namespaced_pod.assert_called_once()
    assert api.create_namespaced_pod.call_args.kwargs["namespace"] == "samplehost-performers"
    container = api.create_namespaced_pod.call_args.kwargs["body"]["spec"]["containers"][0]
    assert {e["name"]: e["value"] for e in container["env"]} == {
        "BACKEND": "claude_code", "OTHER": "value", "SELFHOSTED_ROUTING_CONFIG": ROUTING_PATH,
    }
    assert {call[0] for call in api.mock_calls} == {"create_namespaced_pod", "read_namespaced_pod"}
