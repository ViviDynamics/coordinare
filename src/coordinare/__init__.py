from __future__ import annotations

import logging
import os

import structlog


def configure_logging(log_level: str | None = None) -> None:
    selected_level = log_level if log_level is not None else os.getenv("COORDINARE_LOG_LEVEL", "INFO")
    level_name = selected_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


configure_logging()
