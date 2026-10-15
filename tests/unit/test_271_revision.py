from __future__ import annotations

import json
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinare.config import ProjectConfiguration
from coordinare.lib.redaction import redact_secrets
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_db import COLUMNS
from coordinare.services.otlp_export import OtlpExporter, log_payload
from coordinare.services.otlp_settings import OtlpConfig
from coordinare.services.otlp_wire import OtlpWire
from coordinare.start_checks import StartCheckError, validate_start_time
from tests.unit.test_271_otlp import ENDPOINT, row

CANARY = "PrivateTelemetryCanary271"
PROXY = "http://proxy.example:3128"
PLAIN_ENDPOINT = "http://collector.example/v1/logs"
PROXY_VARIABLES = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)
BASE_CONFIG = {
    "github_org": "garden", "github_token": "ghp_fixturetoken", "human_reviewers": ["operator"],
}


@pytest.mark.parametrize("scheme", ["Bearer ", "Basic ", "token ", ""])
def test_redaction_consumes_the_entire_authorization_value(scheme: str) -> None:
    assert redact_secrets(f"Authorization: {scheme}abc123") == "[REDACTED]"


@pytest.mark.parametrize("service_name", ["coordinare", "configured"])
def test_empty_service_name_uses_default_without_disabling_export(service_name: str) -> None:
    resolved = OtlpConfig(logs_endpoint=ENDPOINT, service_name=service_name).resolve(
        {"OTEL_SERVICE_NAME": ""},
    )
    assert resolved.service_name == OtlpConfig().service_name
    assert resolved.target == ENDPOINT


@pytest.fixture
def clean_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.usefixtures("clean_proxy_environment")
@pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
def test_plain_http_auth_is_refused_when_a_proxy_applies(
    variable: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(variable, PROXY)
    config = OtlpConfig(
        logs_endpoint=PLAIN_ENDPOINT, insecure=True, headers={"Authorization": CANARY},
    )
    with pytest.raises(ValueError, match=r"auth headers.*proxy.*cleartext") as error:
        OtlpWire(config, None)
    assert CANARY not in "".join(traceback.format_exception(error.value))
    assert PROXY not in str(error.value)


@pytest.mark.usefixtures("clean_proxy_environment")
@pytest.mark.parametrize("no_proxy", ["collector.example", ".example", "*"])
def test_no_proxy_allows_explicit_insecure_auth(
    no_proxy: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    wire = OtlpWire(OtlpConfig(
        logs_endpoint=PLAIN_ENDPOINT, insecure=True, headers={"Authorization": CANARY},
    ), None)
    wire.close()


@pytest.mark.usefixtures("clean_proxy_environment")
@pytest.mark.parametrize("endpoint", [PLAIN_ENDPOINT, ENDPOINT])
def test_proxy_still_supports_plain_http_without_auth_and_https(
    endpoint: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    headers = {"Authorization": CANARY} if endpoint == ENDPOINT else {}
    wire = OtlpWire(OtlpConfig(logs_endpoint=endpoint, headers=headers), None)
    wire.close()


@pytest.mark.usefixtures("clean_proxy_environment")
def test_proxy_enabled_after_start_cannot_receive_plain_http_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = OtlpWire(OtlpConfig(
        logs_endpoint=PLAIN_ENDPOINT, insecure=True, headers={"Authorization": CANARY},
    ), None)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    with patch("coordinare.services.otlp_wire.httpx.Client") as factory:
        with pytest.raises(ValueError, match=r"auth headers.*proxy.*cleartext"):
            wire.send(log_payload([row()], {}))
        factory.assert_not_called()


@pytest.mark.parametrize("field", ["protocol", "endpoint", "headers", "queue_size"])
def test_invalid_yaml_telemetry_is_rejected_without_secrets(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE_CONFIG, "observability": {"otlp": {field: CANARY}}}))
    with pytest.raises(ValueError, match="Invalid OTLP settings") as error:
        ProjectConfiguration.from_yaml(path)
    assert CANARY not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("loaded", [False, True])
def test_telemetry_start_check_never_exposes_header_values(
    loaded: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.delenv("OTLP_REVISION_MISSING_KEY", raising=False)
    raw = {**BASE_CONFIG, "observability": {"otlp": {"headers": {
        "API_KEY": CANARY + "${OTLP_REVISION_MISSING_KEY}",
    }}}}
    config = ProjectConfiguration.model_validate(raw)
    with pytest.raises(StartCheckError, match="OTLP_REVISION_MISSING_KEY") as error:
        validate_start_time(config, raw=None if loaded else raw)
    assert CANARY not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
async def test_replay_fallback_uses_schema_column_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("coordinare.services.event_store.COLUMNS", tuple(reversed(COLUMNS)))
    exporter = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT))
    store = EventStore(tmp_path / "events.db")
    store.set_exporter(exporter)
    duplicate = row(data={"transcript_seq": 1})
    accepted = row(message="new committed row", data={"transcript_seq": 2})
    with patch.object(exporter, "offer", return_value=True) as offered:
        await store.start()
        try:
            assert store.emit(duplicate)
            await store.flush()
            offered.reset_mock()
            assert store.emit(duplicate)
            assert store.emit(accepted)
            await store.flush()
            offered.assert_called_once()
            actual = offered.call_args.args[0]
            assert actual == {**accepted, "data": json.dumps(accepted["data"])}
            assert store.health()["rows"] == len({duplicate["message"], accepted["message"]})
        finally:
            await store.close()
