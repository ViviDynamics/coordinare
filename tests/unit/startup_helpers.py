"""Exercise preflight and bootstrap without binding HTTP listeners."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from coordinare import __main__ as cli


def run_preflight_without_listeners(monkeypatch, bootstrap) -> None:
    """Keep event-store and initializer behavior real; isolate service creation."""
    bootstrap.return_value = {"github_service": MagicMock(field_cache={})}
    monkeypatch.setattr(cli, "_bootstrap_services", bootstrap)

    async def initialize(config, config_path, coordinare_config, config_mode, event_store):
        daemon = MagicMock()
        # The real daemon always carries its stop event (299: dispatch refuses once stopping).
        daemon.state = {"activity_log": MagicMock(), "stop_event": asyncio.Event()}
        daemon.start = AsyncMock()
        await cli._initialize_daemon(config, config_path, coordinare_config, daemon, {})

    monkeypatch.setattr(cli, "_run_with_events", initialize)
