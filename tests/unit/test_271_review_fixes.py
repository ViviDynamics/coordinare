from __future__ import annotations

import json
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from coordinare.__main__ import _run
from coordinare.config import CoordinareConfiguration, ProjectConfiguration
from coordinare.services.event_store import _safe_row
from coordinare.services.event_store_settings import EventStoreConfig
from coordinare.services.otlp_export import MAX_DATA_BYTES, OtlpExporter, log_payload
from coordinare.services.otlp_settings import ObservabilityConfig, OtlpConfig
from coordinare.services.otlp_wire import OtlpWire
from coordinare.start_checks import placeholder_problems
from tests.unit.test_271_otlp import ENDPOINT, row

CANARY = 'CanaryPrivate271NeverEcho'
SECRET_FIELDS = [
    {'endpoint': f'https://user:{CANARY}@collector'},
    {'endpoint': f'https://collector?token={CANARY}'},
    {'headers': f'Authorization={CANARY}%0d%0a'},
    {'headers': {'x-api-key': CANARY + '\n'}},
]


@pytest.mark.parametrize('fields', SECRET_FIELDS)
@pytest.mark.parametrize('source', ['env', 'yaml', 'multi'])
def test_settings_errors_never_echo_values(
    fields: dict[str, Any], source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, 'cwd', lambda: tmp_path)
    with capture_logs() as logs, pytest.raises(ValueError) as error:
        if source == 'env':
            OtlpConfig().resolve({'OTEL_EXPORTER_OTLP_' + key.upper(): value for key, value in fields.items()})
        elif source == 'yaml':
            path = tmp_path / 'config.yaml'
            path.write_text(yaml.safe_dump({'observability': {'otlp': fields}}))
            ProjectConfiguration.from_yaml(path)
        else:
            CoordinareConfiguration.model_validate({'global_config': {'observability': {'otlp': fields}}, 'symphonies': []})
    assert CANARY not in str(error.value)
    assert CANARY not in ''.join(traceback.format_exception(error.value))
    assert CANARY not in json.dumps(logs)
    assert next(iter(fields)) in str(error.value)


@pytest.mark.parametrize('token', [*(f'xox{kind}-1234567890-PrivateKey271' for kind in 'abprs'), 'glpat-PrivateKey2710123456789'])
def test_export_redacts_vendor_tokens(token: str) -> None:
    safe = _safe_row(json.dumps(row(message=token, data={'note': token})))
    assert token not in json.dumps(log_payload([safe], {}))


@pytest.mark.parametrize('name', ['x-honeycomb-team', 'x-api-key', 'api-key', 'api_key', 'access_token', 'client_secret'])
@pytest.mark.parametrize('separator', ['=', ': '])
def test_export_redacts_named_credentials(name: str, separator: str) -> None:
    safe = _safe_row(json.dumps(row(message=f'{name}{separator}{CANARY}', data={name: CANARY, 'note': 'ordinary text'})))
    exported = json.dumps(log_payload([safe], {}))
    assert CANARY not in exported
    assert 'ordinary text' in exported


def test_redaction_keeps_ordinary_text() -> None:
    message = 'token counts, api-key documentation, x-api-key format and password policy'
    safe = _safe_row(json.dumps(row(message=message, data={'token_count': 12, 'team': 'garden'})))
    assert safe['message'] == message
    assert json.loads(safe['data']) == {'token_count': 12, 'team': 'garden'}


def test_oversize_data_retains_bounded_json_prefix() -> None:
    payload = log_payload([row(data=json.dumps({'message': 'useful context ' * MAX_DATA_BYTES}))], {})
    record = payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    data = next(item['value']['stringValue'] for item in record['attributes'] if item['key'] == 'data')
    assert len(data.encode()) <= MAX_DATA_BYTES
    clipped = json.loads(data)
    assert clipped['truncated'] is True
    assert 'useful context' in clipped['preview']


@pytest.mark.parametrize('field', ['endpoint', 'logs_endpoint'])
def test_auth_requires_tls_or_explicit_insecure(field: str) -> None:
    fields = {field: 'http://collector', 'headers': {'x-tenant-key': CANARY}}
    with pytest.raises(ValueError, match='insecure') as error:
        OtlpConfig.model_validate(fields)
    assert CANARY not in str(error.value)
    assert OtlpConfig.model_validate({**fields, 'insecure': True}).target


def test_empty_environment_clears_yaml_endpoint() -> None:
    assert OtlpConfig(endpoint='https://yaml').resolve({'OTEL_EXPORTER_OTLP_ENDPOINT': ''}).target is None


def test_http_client_honours_proxy_environment() -> None:
    wire = OtlpWire(OtlpConfig(logs_endpoint=ENDPOINT), None)
    client = MagicMock()
    response = httpx.Response(200, json={}, request=httpx.Request('POST', ENDPOINT))
    client.stream.return_value.__enter__.return_value = response
    with patch('coordinare.services.otlp_wire.httpx.Client', return_value=client) as factory:
        assert wire.send(log_payload([row()], {})) == 0
    assert factory.call_args.kwargs['trust_env'] is True


@pytest.mark.parametrize('content', [b'ok', b'<html>accepted</html>', b'[]', b'{"partialSuccess": null}', b'{"partialSuccess": {"rejectedLogRecords": -1}}'])
def test_non_json_success_is_delivered(content: bytes) -> None:
    wire = OtlpWire(OtlpConfig(logs_endpoint=ENDPOINT), httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    try:
        assert wire.send(log_payload([row()], {})) == 0
    finally:
        wire.close()


def test_batch_uses_remaining_deadline_after_notification() -> None:
    exporter = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT, flush_seconds=1))
    exporter.offer(row())
    with patch('coordinare.services.otlp_export.time.monotonic', side_effect=[0, 0.75, 1]), patch.object(exporter._condition, 'wait') as wait:
        assert len(exporter._batch()) == 1
    wait.assert_called_once_with(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['protocol_env', 'extra', 'endpoint_env', 'headers_env'])
async def test_bad_environment_telemetry_disables_export_without_stopping_daemon(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', ENDPOINT)
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_HEADERS', '')
    if failure == 'protocol_env':
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_PROTOCOL', CANARY)
    if failure == 'endpoint_env':
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', f'https://user:{CANARY}@collector')
    if failure == 'headers_env':
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_HEADERS', CANARY)
    if failure == 'extra':
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_PROTOCOL', 'grpc')
    config: Any = SimpleNamespace(state_file_path=tmp_path / 'state.json', event_store=EventStoreConfig(), observability=ObservabilityConfig())
    observed = []

    async def serve(*args: Any) -> None:
        observed.append(args[-1].health())

    with patch('coordinare.__main__.load_unexpanded_config', return_value=None), patch('coordinare.__main__.validate_start_time'), patch('coordinare.__main__._run_with_events', side_effect=serve), patch('coordinare.services.otlp_wire._logs_proto', None), capture_logs() as logs:
        await _run(config)
    assert observed[0]['export_error']
    assert CANARY not in json.dumps(observed)
    assert CANARY not in json.dumps(logs)
    assert any(entry['event'] == 'otlp.export_disabled' for entry in logs)


def test_ci_runs_installed_binary_wire_and_license_gates() -> None:
    root = Path(__file__).resolve().parents[2]
    for filename in ('pr-ci.yml', 'main-branch-build.yml'):
        workflow = yaml.safe_load((root / '.github/workflows' / filename).read_text())
        steps = workflow['jobs']['test']['steps']
        gate = next(step for step in steps if step['name'] == 'Verify OTLP wire protocols and licences')
        command = gate['run']
        assert 'uv sync --extra otlp' in command
        assert 'import grpc' in command
        assert 'test_271_otlp_wire.py' in command
        assert 'test_142_license_and_legal_posture.py' in command


@pytest.mark.parametrize('message', [f'Authorization: Bearer {CANARY}', f'Authorization=Basic {CANARY}', f'x-api-key="{CANARY} with spaces"'])
def test_export_redacts_complete_header_values(message: str) -> None:
    safe = _safe_row(json.dumps(row(message=message)))
    assert CANARY not in json.dumps(log_payload([safe], {}))


@pytest.mark.parametrize('loaded', [False, True])
def test_secret_header_mapping_participates_in_placeholder_checks(loaded: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('OTLP_271_MISSING_KEY', raising=False)
    raw = {'otlp': {'headers': {'API_KEY': '${OTLP_271_MISSING_KEY}'}}}
    value = ObservabilityConfig.model_validate(raw) if loaded else raw
    assert placeholder_problems(ObservabilityConfig, value)
