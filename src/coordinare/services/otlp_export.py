from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from coordinare.lib.redaction import redact_state_value
from coordinare.services.export_metrics import EXPORT_METRICS
from coordinare.services.otlp_settings import DEFAULT_BACKOFF_MAX_SECONDS, OtlpConfig
from coordinare.services.otlp_wire import OtlpWire

if TYPE_CHECKING:
    import httpx

NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MICROSECOND = 1000
MAX_DATA_BYTES = 16 * 1024
TRUNCATION_SHRINK_FACTOR = 2
SEND_ATTEMPTS = 3
BACKOFF_MULTIPLIER = 2
SHUTDOWN_SECONDS = 1.0
SEVERITIES = {
    "trace": 1,
    "debug": 5,
    "info": 9,
    "warn": 13,
    "warning": 13,
    "error": 17,
    "exception": 17,
    "fatal": 21,
    "critical": 21,
}
ATTRIBUTES = ("symphony", "card_id", "run_id", "stage", "source", "kind")
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR
EPOCH_ORDINAL = datetime.fromisoformat("1970-01-01T00:00:00+00:00").toordinal()


def _timestamp(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    offset = stamp.utcoffset()
    seconds = (
        (stamp.toordinal() - EPOCH_ORDINAL) * SECONDS_PER_DAY
        + stamp.hour * SECONDS_PER_HOUR
        + stamp.minute * SECONDS_PER_MINUTE
        + stamp.second
    )
    if offset is not None:
        seconds -= int(offset.total_seconds())
    return str(seconds * NANOSECONDS_PER_SECOND + stamp.microsecond * NANOSECONDS_PER_MICROSECOND)


def _attributes(values: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def log_payload(rows: list[dict[str, Any]], resource: dict[str, str]) -> dict[str, Any]:
    records = []
    for row in rows:
        data = str(row.get("data") or "{}")
        if len(data.encode()) > MAX_DATA_BYTES:
            preview = data[:MAX_DATA_BYTES]
            data = json.dumps({"truncated": True, "preview": preview})
            while len(data.encode()) > MAX_DATA_BYTES:
                preview = preview[:len(preview) // TRUNCATION_SHRINK_FACTOR]
                data = json.dumps({"truncated": True, "preview": preview})
        records.append(
            {
                "timeUnixNano": _timestamp(row["ts"]),
                "severityNumber": SEVERITIES.get(row["level"], SEVERITIES["info"]),
                "severityText": row["level"].upper(),
                "body": {"stringValue": row["message"]},
                "attributes": _attributes(
                    {**{key: str(row.get(key) or "") for key in ATTRIBUTES}, "data": data},
                ),
            },
        )
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attributes(resource)},
                "scopeLogs": [{"scope": {"name": "coordinare.event_store"}, "logRecords": records}],
            },
        ],
    }


def _resource(config: OtlpConfig) -> dict[str, str]:
    try:
        package_version = version("coordinare")
    except PackageNotFoundError:
        package_version = "dev"
    resource = {"service.name": config.service_name, "service.version": package_version}
    for variable, attribute in {
        "OTEL_DEPLOYMENT_ENVIRONMENT": "deployment.environment.name",
        "POD_NAMESPACE": "k8s.namespace.name",
        "POD_NAME": "k8s.pod.name",
    }.items():
        if os.environ.get(variable):
            resource[attribute] = os.environ[variable]
    return {key: str(redact_state_value(value)) for key, value in resource.items()}


class OtlpExporter:
    """Independent bounded worker. No logs, disk access or daemon loop callbacks."""

    def __init__(self, config: OtlpConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._wire = OtlpWire(config, transport)
        self._resource = _resource(config)
        self._queue: deque[tuple[str, int]] = deque()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._count = 0
        self._bytes = 0
        self._dropped = 0
        self._failed = 0
        self._discarded = False

    def start(self) -> None:
        if self._thread is None and self.config.target:
            self._thread = threading.Thread(target=self._run, name="otlp-export", daemon=True)
            self._thread.start()

    def _drop(self, count: int) -> None:
        self._dropped += count
        EXPORT_METRICS.export_dropped.inc(count)

    def offer(self, row: dict[str, Any]) -> bool:
        """Only the store writer calls this, after redaction and successful commit."""
        raw = json.dumps(row, ensure_ascii=True)
        size = len(raw)
        with self._condition:
            if (
                not self.config.target
                or self._stop.is_set()
                or self._count >= self.config.queue_size
                or self._bytes + size > self.config.queue_bytes
            ):
                self._drop(1)
                return False
            self._queue.append((raw, size))
            self._count += 1
            self._bytes += size
            self._condition.notify()
            return True

    def _batch(self) -> list[tuple[str, int]]:
        deadline = time.monotonic() + self.config.flush_seconds
        with self._condition:
            while len(self._queue) < self.config.batch_size and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            batch: list[tuple[str, int]] = []
            while self._queue and len(batch) < self.config.batch_size:
                batch.append(self._queue.popleft())
            return batch

    def _wait_backoff(self, delay: float) -> None:
        self._stop.wait(delay)

    def _send(self, rows: list[dict[str, Any]]) -> int:
        payload = log_payload(rows, self._resource)
        delay = min(self.config.backoff_seconds, DEFAULT_BACKOFF_MAX_SECONDS)
        for attempt in range(SEND_ATTEMPTS):
            try:
                return min(self._wire.send(payload), len(rows))
            except Exception as exc:
                with self._condition:
                    self._failed += 1
                    EXPORT_METRICS.export_failed.inc()
                if not self._wire.retryable(exc):
                    break
            if self._stop.is_set() or attempt + 1 == SEND_ATTEMPTS:
                break
            self._wait_backoff(delay)
            delay = min(delay * BACKOFF_MULTIPLIER, DEFAULT_BACKOFF_MAX_SECONDS)
            if self._stop.is_set():
                break
        return len(rows)

    def _run(self) -> None:
        try:
            while True:
                batch = self._batch()
                if not batch:
                    if self._stop.is_set():
                        return
                    continue
                try:
                    lost = self._send([json.loads(raw) for raw, _ in batch])
                except Exception:
                    lost = len(batch)
                with self._condition:
                    if not self._discarded:
                        self._drop(lost)
                        self._count -= len(batch)
                        self._bytes -= sum(size for _, size in batch)
        finally:
            with contextlib.suppress(Exception):
                self._wire.close()

    def health(self) -> dict[str, Any]:
        with self._condition:
            return {"export_dropped": self._dropped, "export_failed": self._failed}

    async def close(self, *, grace_seconds: float = SHUTDOWN_SECONDS) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, grace_seconds)
        with self._condition:
            if self._count:
                self._drop(self._count)
                self._count = self._bytes = 0
                self._queue.clear()
                self._discarded = True
