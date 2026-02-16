from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from coordinare.lib.redaction import redact_mapping


def _redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in event_dict.items():
        if isinstance(value, Mapping):
            safe[key] = redact_mapping(value)
        else:
            safe[key] = value
    return redact_mapping(safe)


def configure_logging(log_level: str | None = None, *, structured: bool | None = None) -> None:
    selected_level = log_level if log_level is not None else os.getenv("COORDINARE_LOG_LEVEL", "INFO")
    level_name = selected_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    structured_enabled = (
        structured
        if structured is not None
        else os.getenv("COORDINARE_OUTPUT_MODE", "human").strip().lower() == "structured"
    )

    logging.basicConfig(level=level, format="%(message)s")
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if structured_enabled else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


configure_logging()
