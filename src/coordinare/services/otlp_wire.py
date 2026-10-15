from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from httpx._utils import URLPattern, get_environment_proxies

if TYPE_CHECKING:
    from coordinare.services.otlp_settings import OtlpConfig

_protobuf_json: Any = None
_logs_proto: Any = None
_grpc: Any = None

try:
    _protobuf_json = importlib.import_module("google.protobuf.json_format")
    _logs_proto = importlib.import_module("opentelemetry.proto.collector.logs.v1.logs_service_pb2")
except ImportError:
    _protobuf_json = None
    _logs_proto = None
try:
    _grpc = importlib.import_module("grpc")
except ImportError:
    _grpc = None

MISSING_BINDINGS = "Install coordinare[otlp] to use http/protobuf or grpc export"
MAX_RESPONSE_BYTES = 64 * 1024
RESPONSE_ERROR = "OTLP collector response exceeds its byte budget"
PARTIAL_ERROR = "Invalid OTLP collector rejection count"
PROXY_AUTH_ERROR = "OTLP auth headers cannot use a proxy with a plain HTTP endpoint: headers would be sent in cleartext; use HTTPS or NO_PROXY"
RETRYABLE_HTTP = frozenset({429, 502, 503, 504})
RETRYABLE_GRPC = frozenset(
    {
        "CANCELLED",
        "DEADLINE_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNAVAILABLE",
        "DATA_LOSS",
    },
)
GRPC_METHOD = "/opentelemetry.proto.collector.logs.v1.LogsService/Export"


def _validate_proxy_auth(config: OtlpConfig) -> None:
    if config.protocol == "grpc" or not config.headers.get_secret_value() or not config.target:
        return
    target = httpx.URL(config.target)
    if target.scheme != "http":
        return
    # Use the same matching and precedence as httpx, including NO_PROXY and ALL_PROXY.
    mounts = {URLPattern(key): value for key, value in get_environment_proxies().items()}
    proxy = next((mounts[key] for key in sorted(mounts) if key.matches(target)), None)
    if proxy is not None:
        raise ValueError(PROXY_AUTH_ERROR)


class OtlpWire:
    """HTTP JSON has no extra dependencies; binary transports use generated bindings."""

    def __init__(self, config: OtlpConfig, transport: httpx.BaseTransport | None) -> None:
        if config.protocol != "http/json" and (_logs_proto is None or _protobuf_json is None):
            raise ValueError(MISSING_BINDINGS)
        if config.protocol == "grpc" and _grpc is None:
            raise ValueError(MISSING_BINDINGS)
        self.config = config
        self.transport = transport
        if transport is None:
            _validate_proxy_auth(config)
        self.client: httpx.Client | None = None
        self.channel: Any = None

    def send(self, payload: dict[str, Any]) -> int:
        if self.config.protocol == "grpc":
            response = self._send_grpc(payload)
            return self._rejected(response.partial_success.rejected_log_records)
        binary = self.config.protocol == "http/protobuf"
        request = self._protobuf(payload).SerializeToString() if binary else None
        if self.client is None:
            if self.transport is None:
                _validate_proxy_auth(self.config)
            self.client = httpx.Client(
                transport=self.transport, timeout=self.config.timeout_seconds, trust_env=True,
            )
        headers = httpx.Headers(self.config.headers.get_secret_value())
        headers["Content-Type"] = "application/x-protobuf" if binary else "application/json"
        with self.client.stream(
            "POST",
            str(self.config.target),
            headers=headers,
            content=request,
            json=None if binary else payload,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError(RESPONSE_ERROR)
                content.extend(chunk)
        if not content:
            return 0
        if binary:
            result = _logs_proto.ExportLogsServiceResponse.FromString(bytes(content))
            return self._rejected(result.partial_success.rejected_log_records)
        try:
            result_json = httpx.Response(response.status_code, content=bytes(content)).json()
            return self._rejected(result_json.get("partialSuccess", {}).get("rejectedLogRecords", 0))
        except (ValueError, TypeError, AttributeError):
            return 0

    @staticmethod
    def _rejected(value: Any) -> int:
        rejected = int(value)
        if rejected < 0:
            raise ValueError(PARTIAL_ERROR)
        return rejected

    def _protobuf(self, payload: dict[str, Any]) -> Any:
        return _protobuf_json.ParseDict(payload, _logs_proto.ExportLogsServiceRequest())

    def _send_grpc(self, payload: dict[str, Any]) -> Any:
        endpoint = urlsplit(str(self.config.target))
        if self.channel is None:
            options = [("grpc.max_receive_message_length", MAX_RESPONSE_BYTES)]
            if endpoint.scheme == "https":
                self.channel = _grpc.secure_channel(
                    endpoint.netloc, _grpc.ssl_channel_credentials(), options=options,
                )
            else:
                self.channel = _grpc.insecure_channel(endpoint.netloc, options=options)
        call = self.channel.unary_unary(
            GRPC_METHOD,
            request_serializer=lambda value: value.SerializeToString(),
            response_deserializer=_logs_proto.ExportLogsServiceResponse.FromString,
        )
        return call(
            self._protobuf(payload),
            timeout=self.config.timeout_seconds,
            metadata=[(key.lower(), value) for key, value in self.config.headers.get_secret_value().items()],
        )

    @staticmethod
    def retryable(error: Exception) -> bool:
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in RETRYABLE_HTTP
        if isinstance(error, httpx.TransportError):
            return True
        if _grpc is not None and isinstance(error, _grpc.RpcError):
            return str(error.code().name) in RETRYABLE_GRPC
        return False

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.channel is not None:
            self.channel.close()
