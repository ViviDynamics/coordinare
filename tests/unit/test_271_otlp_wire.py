from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from coordinare.services import otlp_wire
from coordinare.services.otlp_export import log_payload
from coordinare.services.otlp_settings import OtlpConfig
from tests.unit.test_271_otlp import ENDPOINT, row


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
def test_binary_protocol_requires_extra(protocol: str) -> None:
    with (
        patch.object(otlp_wire, "_logs_proto", None),
        pytest.raises(ValueError, match=r"coordinare\[otlp\]"),
    ):
        otlp_wire.OtlpWire(OtlpConfig(logs_endpoint=ENDPOINT, protocol=protocol), None)


def test_http_protobuf_round_trip_or_missing_extra_error() -> None:
    config = OtlpConfig(logs_endpoint=ENDPOINT, protocol="http/protobuf")
    proto = otlp_wire._logs_proto
    if proto is None:
        with pytest.raises(ValueError, match=r"coordinare\[otlp\]"):
            otlp_wire.OtlpWire(config, None)
        return
    records: list[Any] = []

    def collect(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/x-protobuf"
        records.append(proto.ExportLogsServiceRequest.FromString(request.content))
        return httpx.Response(200, content=proto.ExportLogsServiceResponse().SerializeToString())

    wire = otlp_wire.OtlpWire(config, httpx.MockTransport(collect))
    try:
        assert wire.send(log_payload([row()], {"service.name": "coordinare"})) == 0
        assert records[0].resource_logs[0].scope_logs[0].log_records[0].body.string_value == "hello"
    finally:
        wire.close()


@pytest.mark.parametrize("endpoint", ["http://collector:4317", "https://collector:4317"])
def test_grpc_uses_rpc_timeout_metadata_and_security(endpoint: str) -> None:
    config = OtlpConfig(endpoint=endpoint, protocol="grpc", insecure=endpoint.startswith("http://"), headers={"Authorization": "Bearer secret", "x-tenant": "samplehost"})
    if otlp_wire._logs_proto is None:
        with pytest.raises(ValueError, match=r"coordinare\[otlp\]"):
            otlp_wire.OtlpWire(config, None)
        return
    grpc = MagicMock()
    channel = MagicMock()
    grpc.secure_channel.return_value = channel
    grpc.insecure_channel.return_value = channel
    channel.unary_unary.return_value.return_value = (
        otlp_wire._logs_proto.ExportLogsServiceResponse()
    )
    with patch.object(otlp_wire, "_grpc", grpc):
        wire = otlp_wire.OtlpWire(config, None)
        assert wire.send(log_payload([row()], {})) == 0
        channel.unary_unary.assert_called_once()
        call = channel.unary_unary.return_value
        assert call.call_args.kwargs == {
            "timeout": config.timeout_seconds,
            "metadata": [("authorization", "Bearer secret"), ("x-tenant", "samplehost")],
        }
        assert channel.unary_unary.call_args.args[0] == otlp_wire.GRPC_METHOD
        serializers = channel.unary_unary.call_args.kwargs
        encoded = serializers["request_serializer"](call.call_args.args[0])
        decoded = otlp_wire._logs_proto.ExportLogsServiceRequest.FromString(encoded)
        assert decoded.resource_logs[0].scope_logs[0].log_records[0].body.string_value == "hello"
        response_bytes = otlp_wire._logs_proto.ExportLogsServiceResponse().SerializeToString()
        assert serializers["response_deserializer"](response_bytes).partial_success.rejected_log_records == 0
        selected = grpc.secure_channel if endpoint.startswith("https") else grpc.insecure_channel
        selected.assert_called_once()
        wire.close()
        channel.close.assert_called_once()


@pytest.mark.parametrize(
    ("content", "oversize"),
    [
        (b"x" * (otlp_wire.MAX_RESPONSE_BYTES + 1), True),
        (json.dumps({"partialSuccess": {"rejectedLogRecords": -1}}).encode(), False),
    ],
)
def test_collector_response_is_bounded_and_validated(content: bytes, oversize: bool) -> None:
    wire = otlp_wire.OtlpWire(
        OtlpConfig(logs_endpoint=ENDPOINT),
        httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
    )
    try:
        if oversize:
            with pytest.raises(ValueError):
                wire.send(log_payload([row()], {}))
        else:
            assert wire.send(log_payload([row()], {})) == 0
    finally:
        wire.close()
