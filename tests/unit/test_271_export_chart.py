from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import yaml

HELM = shutil.which("helm")
CHART = "deploy/helm/coordinare"
TIMEOUT_SECONDS = 30


def render(values: dict[str, Any]) -> list[dict[str, Any]]:
    assert HELM is not None, "helm is required for chart rendering"
    args = [HELM, "template", "coordinare", CHART]
    for key, value in values.items():
        args.extend(["--set-json", f"{key}={json.dumps(value)}"])
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_export_chart_off_by_default_and_renders_config() -> None:
    default = render({})
    config = next(doc for doc in default if doc["kind"] == "ConfigMap")
    settings = yaml.safe_load(config["data"]["config.yaml"])
    assert settings["observability"]["otlp"]["endpoint"] == ""
    assert settings["observability"]["metrics_enabled"] is True
    service = next(doc for doc in default if doc["kind"] == "Service")
    assert "metrics" not in {port["name"] for port in service["spec"]["ports"]}
    objects = render(
        {"observability.otlp.endpoint": "http://otel-collector:4318", "metrics.expose": True},
    )
    config = next(doc for doc in objects if doc["kind"] == "ConfigMap")
    settings = yaml.safe_load(config["data"]["config.yaml"])
    assert settings["observability"]["otlp"]["endpoint"] == "http://otel-collector:4318"
    assert settings["observability"]["metrics_enabled"] is True
    pod = next(doc for doc in objects if doc["kind"] == "StatefulSet")
    env = pod["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(item["name"] == "POD_NAMESPACE" and "valueFrom" in item for item in env)


def test_config_overrides_chart_defaults() -> None:
    objects = render({"config.observability.otlp.logs_endpoint": "https://custom/v1/logs"})
    config = next(doc for doc in objects if doc["kind"] == "ConfigMap")
    assert (
        yaml.safe_load(config["data"]["config.yaml"])["observability"]["otlp"]["logs_endpoint"]
        == "https://custom/v1/logs"
    )


def test_export_credentials_stay_in_the_secret() -> None:
    sentinel = 'Bearer SENTINEL-COLLECTOR-SECRET'
    objects = render({'secrets.OTEL_EXPORTER_OTLP_HEADERS': 'Authorization=' + sentinel})
    for obj in objects:
        if obj['kind'] != 'Secret':
            assert sentinel not in yaml.safe_dump(obj)


def test_plaintext_headers_fail_rendering() -> None:
    for key in ('observability.otlp.headers', 'config.observability.otlp.headers'):
        assert HELM is not None
        result = subprocess.run(
            [HELM, 'template', 'coordinare', CHART, '--set-json', key + '={"Authorization":"canary271"}'],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
        assert result.returncode != 0
        assert 'OTEL_EXPORTER_OTLP_HEADERS' in result.stderr
        assert 'canary271' not in result.stderr


def test_headers_secret_reference_is_not_in_configmap() -> None:
    objects = render({'observability.otlp.headersSecret.name': 'collector-auth', 'observability.otlp.headersSecret.key': 'headers'})
    pod = next(doc for doc in objects if doc['kind'] == 'StatefulSet')
    env = pod['spec']['template']['spec']['containers'][0]['env']
    assert next(item for item in env if item['name'] == 'OTEL_EXPORTER_OTLP_HEADERS')['valueFrom'] == {
        'secretKeyRef': {'name': 'collector-auth', 'key': 'headers'},
    }
    config = next(doc for doc in objects if doc['kind'] == 'ConfigMap')
    assert 'headersSecret' not in config['data']['config.yaml']


def test_loadbalancer_does_not_publish_health_by_default() -> None:
    objects = render({'dashboard.service.type': 'LoadBalancer'})
    service = next(doc for doc in objects if doc['kind'] == 'Service')
    assert 'metrics' not in {port['name'] for port in service['spec']['ports']}
