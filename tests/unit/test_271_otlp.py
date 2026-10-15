from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from coordinare.__main__ import _run
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_settings import EventStoreConfig
from coordinare.services.otlp_export import OtlpExporter, log_payload
from coordinare.services.otlp_settings import ObservabilityConfig, OtlpConfig

WAIT_SECONDS = 5.0
BATCH_SIZE = 2
TOKEN = "ghp_" + "a" * 36
ENDPOINT = "https://collector.example/v1/logs"


def row(**fields: Any) -> dict[str, Any]:
    return {
        "ts": "2026-10-15T12:00:00+00:00",
        "symphony": "garden",
        "card_id": "271",
        "run_id": "run",
        "stage": "implementing",
        "source": "performer",
        "kind": "progress",
        "level": "warning",
        "message": "hello",
        "data": "{}",
        **fields,
    }


async def wait_for(signal: threading.Event) -> None:
    assert await asyncio.to_thread(signal.wait, WAIT_SECONDS)


def test_off_by_default_and_validation() -> None:
    assert OtlpConfig().resolve({}).target is None
    assert ObservabilityConfig().metrics_enabled is True
    for fields in (
        {"endpoint": "ftp://bad"},
        {"protocol": "bad"},
        {"queue_size": 0},
        {"batch_size": 0},
        {"timeout_seconds": 0},
        {"extra": True},
    ):
        with pytest.raises(ValidationError):
            OtlpConfig.model_validate(fields)


def test_environment_precedence_and_headers() -> None:
    config = OtlpConfig(
        endpoint="https://config/base",
        logs_endpoint="https://config/logs",
        service_name="configured",
    )
    resolved = config.resolve(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://env/base/",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ENDPOINT,
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20secret,x-tenant=garden",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_SERVICE_NAME": "daemon",
        },
    )
    assert resolved.target == ENDPOINT
    assert resolved.service_name == "daemon"
    assert resolved.headers.get_secret_value()["Authorization"] == "Bearer secret"
    assert resolved.protocol == "http/json"
    assert (
        OtlpConfig(endpoint="https://config/base/").resolve({}).target
        == "https://config/base/v1/logs"
    )
    assert (
        config.resolve(
            {"OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"},
        ).protocol
        == "grpc"
    )
    assert "secret" not in repr(resolved)


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        ("trace", 1),
        ("debug", 5),
        ("info", 9),
        ("warn", 13),
        ("warning", 13),
        ("error", 17),
        ("exception", 17),
        ("fatal", 21),
        ("critical", 21),
    ],
)
def test_payload_attributes_and_severity(level: str, severity: int) -> None:
    payload = log_payload(
        [row(level=level)], {"service.name": "coordinare", "service.version": "test"},
    )
    resource = payload["resourceLogs"][0]
    assert resource["resource"]["attributes"][0] == {
        "key": "service.name",
        "value": {"stringValue": "coordinare"},
    }
    record = resource["scopeLogs"][0]["logRecords"][0]
    assert record["timeUnixNano"] == "1792065600000000000"
    assert record["severityNumber"] == severity
    assert record["body"] == {"stringValue": "hello"}
    attributes = {item["key"]: item["value"]["stringValue"] for item in record["attributes"]}
    assert attributes == {
        key: str(row()[key])
        for key in ("symphony", "card_id", "run_id", "stage", "source", "kind", "data")
    }


@pytest.mark.asyncio
async def test_writer_exports_only_redacted_rows_and_batches(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    sent = threading.Event()

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sent.set()
        return httpx.Response(200, json={})

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=BATCH_SIZE, flush_seconds=WAIT_SECONDS),
        transport=httpx.MockTransport(collect),
    )
    store = EventStore(tmp_path / "events.db")
    store.set_exporter(exporter)
    try:
        await store.start()
        assert store.emit(row(message=TOKEN, symphony=TOKEN, data={"password": "never-export", "value": TOKEN}))
        assert store.emit(row(message="second"))
        await asyncio.wait_for(store.flush(), WAIT_SECONDS)
        await wait_for(sent)
        payload = json.loads(requests[0].content)
        records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        assert len(records) == BATCH_SIZE
        assert TOKEN not in requests[0].content.decode()
        assert "never-export" not in requests[0].content.decode()
        assert "REDACTED" in records[0]["body"]["stringValue"]
        assert requests[0].headers["content-type"] == "application/json"
        assert str(requests[0].url) == ENDPOINT
        assert store.health()["rows"] == BATCH_SIZE
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_down_collector_does_not_block_writer_and_drops_are_counted(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def collect(_: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(WAIT_SECONDS)
        raise httpx.ConnectError("sensitive collector detail")

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, queue_size=1, batch_size=1),
        transport=httpx.MockTransport(collect),
    )
    store = EventStore(tmp_path / "events.db")
    store.set_exporter(exporter)
    try:
        await store.start()
        assert store.emit(row())
        await wait_for(entered)
        for _ in range(BATCH_SIZE):
            assert store.emit(row())
        await asyncio.wait_for(store.flush(), WAIT_SECONDS)
        assert store.health()["rows"] == BATCH_SIZE + 1
        assert store.health()["export_dropped"] == BATCH_SIZE
    finally:
        release.set()
        await store.close()
    assert store.health()["export_failed"] == 1
    assert "sensitive" not in json.dumps(store.health())


@pytest.mark.asyncio
async def test_failure_backoff_and_recovery() -> None:
    failed, recovered = threading.Event(), threading.Event()
    attempts: list[float] = []

    def collect(_: httpx.Request) -> httpx.Response:
        if not failed.is_set():
            failed.set()
            return httpx.Response(503)
        recovered.set()
        return httpx.Response(200, json={})

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=1), transport=httpx.MockTransport(collect),
    )
    original = exporter._wait_backoff

    def backoff(delay: float) -> None:
        attempts.append(delay)
        original(0)

    with patch.object(exporter, "_wait_backoff", side_effect=backoff):
        exporter.start()
        assert exporter.offer(row())
        await wait_for(failed)
        await wait_for(recovered)
        await exporter.close()
    assert attempts == [exporter.config.backoff_seconds]
    assert exporter.health()["export_failed"] == 1
    assert exporter.health()["export_dropped"] == 0


def test_data_attribute_remains_valid_json_when_bounded() -> None:
    data = json.dumps({"text": "x" * (32 * 1024)})
    record = log_payload([row(data=data)], {})["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    value = next(
        item["value"]["stringValue"] for item in record["attributes"] if item["key"] == "data"
    )
    assert len(value.encode()) <= 16 * 1024
    assert json.loads(value)["truncated"] is True


@pytest.mark.asyncio
async def test_byte_budget_and_shutdown_losses_are_counted() -> None:
    entered, release = threading.Event(), threading.Event()

    def collect(_: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(WAIT_SECONDS)
        return httpx.Response(200, json={})

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=1), transport=httpx.MockTransport(collect),
    )
    exporter.start()
    assert exporter.offer(row())
    await wait_for(entered)
    try:
        await exporter.close(grace_seconds=0)
        assert exporter.health()["export_dropped"] == 1
        assert not exporter.offer(row())
        assert exporter.health()["export_dropped"] == BATCH_SIZE
    finally:
        release.set()
        await exporter.close()
    tiny = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT, queue_bytes=1))
    assert not tiny.offer(row())
    assert tiny.health()["export_dropped"] == 1
    await tiny.close()


@pytest.mark.asyncio
async def test_timed_flush_and_partial_collector_rejection() -> None:
    sent = threading.Event()

    def collect(_: httpx.Request) -> httpx.Response:
        sent.set()
        return httpx.Response(200, json={"partialSuccess": {"rejectedLogRecords": "1"}})

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=BATCH_SIZE, flush_seconds=0.01),
        transport=httpx.MockTransport(collect),
    )
    exporter.start()
    assert exporter.offer(row())
    await wait_for(sent)
    await exporter.close()
    assert exporter.health() == {"export_dropped": 1, "export_failed": 0}


@pytest.mark.asyncio
async def test_retry_exhaustion_counts_failed_attempts_and_lost_rows() -> None:
    attempted = threading.Event()
    backoffs: list[float] = []

    def collect(_: httpx.Request) -> httpx.Response:
        if len(backoffs) == BATCH_SIZE:
            attempted.set()
        raise httpx.ReadTimeout("secret response")

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=1), transport=httpx.MockTransport(collect),
    )
    with patch.object(exporter, "_wait_backoff", side_effect=backoffs.append):
        exporter.start()
        assert exporter.offer(row())
        await wait_for(attempted)
        await exporter.close()
    assert backoffs == [1.0, 2.0]
    assert exporter.health() == {"export_dropped": 1, "export_failed": 3}


@pytest.mark.asyncio
async def test_export_exception_cannot_retry_or_drop_committed_rows(tmp_path: Path) -> None:
    exporter = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT))
    store = EventStore(tmp_path / "events.db")
    store.set_exporter(exporter)
    with patch.object(exporter, "offer", side_effect=RuntimeError("failed")):
        await store.start()
        assert store.emit(row())
        await store.close()
    assert store.health()["rows"] == 1
    assert store.health()["dropped"] == 0


@pytest.mark.asyncio
async def test_replayed_performer_rows_are_not_exported_twice(tmp_path: Path) -> None:
    exporter = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT))
    store = EventStore(tmp_path / "events.db")
    store.set_exporter(exporter)
    with patch.object(exporter, "offer", return_value=True) as offered:
        await store.start()
        for _ in range(BATCH_SIZE):
            assert store.emit(row(data={"transcript_seq": 1}))
        await store.close()
    assert store.health()["rows"] == 1
    assert offered.call_count == 1


def test_resource_version_and_deployment_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_NAMESPACE", "garden")
    exporter = OtlpExporter(OtlpConfig(logs_endpoint=ENDPOINT))
    assert exporter._resource["service.name"] == "coordinare"
    assert exporter._resource["service.version"]
    assert exporter._resource["k8s.namespace.name"] == "garden"


@pytest.mark.asyncio
async def test_permanent_rejection_is_not_retried() -> None:
    sent = threading.Event()
    requests: list[httpx.Request] = []

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 3.0
        sent.set()
        return httpx.Response(400)

    exporter = OtlpExporter(
        OtlpConfig(logs_endpoint=ENDPOINT, batch_size=1), transport=httpx.MockTransport(collect),
    )
    exporter.start()
    assert exporter.offer(row())
    await wait_for(sent)
    await exporter.close()
    assert len(requests) == 1
    assert exporter.health() == {"export_dropped": 1, "export_failed": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_daemon_wires_environment_and_closes_exporter_on_startup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    requests: list[httpx.Request] = []
    sent = threading.Event()

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sent.set()
        return httpx.Response(200, json={})

    def create(config: OtlpConfig) -> OtlpExporter:
        return OtlpExporter(config, transport=httpx.MockTransport(collect))

    async def fail(*args: Any) -> None:
        store = args[-1]
        assert store.emit(row(message=TOKEN))
        await store.flush()
        if enabled:
            await wait_for(sent)
        raise RuntimeError('startup failed')

    for variable in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', 'OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_EXPORTER_OTLP_LOGS_HEADERS', 'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_LOGS_PROTOCOL', 'OTEL_SERVICE_NAME'):
        monkeypatch.delenv(variable, raising=False)
    if enabled:
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', ENDPOINT)
    config: Any = SimpleNamespace(state_file_path=tmp_path / 'state.json', event_store=EventStoreConfig(), observability=ObservabilityConfig(otlp=OtlpConfig(batch_size=1)))
    with (
        patch('coordinare.__main__.load_unexpanded_config', return_value=None),
        patch('coordinare.__main__.validate_start_time'),
        patch('coordinare.__main__._run_with_events', side_effect=fail),
        patch('coordinare.__main__.OtlpExporter', side_effect=create) as factory,
        pytest.raises(RuntimeError, match='startup failed'),
    ):
        await _run(config)
    assert factory.call_count == int(enabled)
    assert len(requests) == int(enabled)
    assert not any(thread.name == 'otlp-export' and thread.is_alive() for thread in threading.enumerate())
    if enabled:
        assert TOKEN not in requests[0].content.decode()


@pytest.mark.parametrize('fields', [{'endpoint': 'http://collector:invalid'}, {'headers': 'Authorization=Bearer%0d%0ainjected'}, {'headers': {'bad\nname': 'value'}}])
def test_invalid_endpoint_and_header_values_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OtlpConfig.model_validate(fields)
