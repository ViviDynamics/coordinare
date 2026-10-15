from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_QUEUE_SIZE = 1024
DEFAULT_SERVICE_NAME = "coordinare"
DEFAULT_QUEUE_BYTES = 4 * 1024 * 1024
DEFAULT_BATCH_SIZE = 128
DEFAULT_FLUSH_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
ENDPOINT_ERROR = "OTLP endpoints must be HTTP or HTTPS URLs without credentials, query or fragment"
PROTOCOL_ERROR = "unsupported protocol; choose http/json, http/protobuf or grpc"
HEADERS_ERROR = "OTLP headers must be comma separated name=value pairs or a mapping"

INSECURE_AUTH_ERROR = "OTLP auth headers require HTTPS unless insecure: true is explicitly set"


def settings_error(error: ValidationError) -> str:
    """Only schema field names and fixed reasons may escape validation."""
    fields = sorted({
        str(item["loc"][0]) if item["loc"] and item["loc"][0] in OtlpConfig.model_fields else "otlp"
        for item in error.errors(include_input=False)
    })
    reason = INSECURE_AUTH_ERROR if any(
        item["type"] == "value_error" and str(item.get("ctx", {}).get("error")) == INSECURE_AUTH_ERROR
        for item in error.errors(include_input=False)
    ) else PROTOCOL_ERROR if fields == ["protocol"] else "invalid setting"
    return "Invalid OTLP settings: " + ", ".join(fields) + " (" + reason + ")"


class OtlpConfig(BaseModel):
    """Explicit opt in; environment overrides the corresponding YAML settings."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    insecure: bool = False
    endpoint: str = ""
    logs_endpoint: str = ""
    headers: Secret[dict[str, str]] = Field(default_factory=lambda: Secret({}))
    protocol: Literal["http/json", "http/protobuf", "grpc"] = "http/json"
    service_name: str = Field(default=DEFAULT_SERVICE_NAME, min_length=1)
    queue_size: int = Field(default=DEFAULT_QUEUE_SIZE, gt=0)
    queue_bytes: int = Field(default=DEFAULT_QUEUE_BYTES, gt=0)
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE, gt=0)
    flush_seconds: float = Field(default=DEFAULT_FLUSH_SECONDS, gt=0, allow_inf_nan=False)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False)
    backoff_seconds: float = Field(default=DEFAULT_BACKOFF_SECONDS, gt=0, allow_inf_nan=False)

    @field_validator("endpoint", "logs_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(ENDPOINT_ERROR) from exc
        if (
            port == 0
            or any(char.isspace() for char in value)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(ENDPOINT_ERROR)
        return value

    @field_validator("headers", mode="before")
    @classmethod
    def parse_headers(cls, value: Any) -> Any:
        if isinstance(value, str):
            headers = {}
            for item in value.split(","):
                if not item.strip():
                    continue
                key, separator, content = item.strip().partition("=")
                if not separator or not key:
                    raise ValueError(HEADERS_ERROR)
                headers[key] = unquote(content)
            value = headers
        if isinstance(value, dict):
            for key, content in value.items():
                if not isinstance(key, str) or not isinstance(content, str) or any(char in key + content for char in "\r\n"):
                    raise ValueError(HEADERS_ERROR)
        return value

    @model_validator(mode="after")
    def require_secure_auth(self) -> OtlpConfig:
        if self.headers.get_secret_value() and self.target and not self.insecure and urlsplit(self.target).scheme == "http":
            raise ValueError(INSECURE_AUTH_ERROR)
        return self

    @property
    def target(self) -> str | None:
        if self.logs_endpoint:
            return self.logs_endpoint
        if self.endpoint:
            return (
                self.endpoint if self.protocol == "grpc" else self.endpoint.rstrip("/") + "/v1/logs"
            )
        return None

    def resolve(self, environ: Mapping[str, str] | None = None) -> OtlpConfig:
        env = os.environ if environ is None else environ
        values = {**self.model_dump(), "headers": self.headers.get_secret_value()}
        names = {
            "endpoint": ("OTEL_EXPORTER_OTLP_ENDPOINT",),
            "logs_endpoint": ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",),
            "headers": ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_HEADERS"),
            "protocol": ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"),
            "service_name": ("OTEL_SERVICE_NAME",),
        }
        for field, variables in names.items():
            for variable in variables:
                if variable in env:
                    values[field] = env[variable]
        values["service_name"] = values["service_name"] or DEFAULT_SERVICE_NAME
        try:
            return OtlpConfig.model_validate(values)
        except ValidationError as exc:
            raise ValueError(settings_error(exc)) from None


class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    otlp: OtlpConfig = Field(default_factory=OtlpConfig)
    metrics_enabled: bool = True

    @field_validator("otlp", mode="before")
    @classmethod
    def validate_otlp(cls, value: Any) -> Any:
        try:
            return OtlpConfig.model_validate(value)
        except ValidationError as exc:
            raise ValueError(settings_error(exc)) from None
