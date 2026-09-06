"""A capturing stand-in for a module-level structlog logger.

``structlog.testing.capture_logs`` cannot intercept a logger that another test
already used (structlog caches processors on first use), which made the log
assertions order dependent. Monkeypatching the module's ``log`` with this
object is deterministic.
"""
from __future__ import annotations


class FakeLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def _record(self, level: str, event: str, **kw) -> None:
        self.entries.append({"log_level": level, "event": event, **kw})

    def debug(self, event: str, **kw) -> None:
        self._record("debug", event, **kw)

    def info(self, event: str, **kw) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw) -> None:
        self._record("warning", event, **kw)

    def error(self, event: str, **kw) -> None:
        self._record("error", event, **kw)

    def exception(self, event: str, **kw) -> None:
        self._record("error", event, **kw)
