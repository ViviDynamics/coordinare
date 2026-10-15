"""Config checks the daemon runs before it starts, shared with the save path (225).

The daemon's boot (``__main__._run``) and the dashboard's save-time candidate check both call
:func:`validate_start_time`, so no save through the dashboard or MCP can write a config the daemon
cannot start with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coordinare.dashboard_auth import validate_dashboard_bind
from coordinare.dashboard_oidc import validate_dashboard_oidc

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration


def validate_start_time(config: ProjectConfiguration) -> None:
    """Raise ``ValueError`` with the operator-facing reason for a config the daemon refuses at boot."""
    validate_dashboard_bind(
        config.dashboard_host, config.dashboard_auth_token, oidc=config.dashboard_oidc,
    )
    if config.dashboard_oidc is not None:
        validate_dashboard_oidc(
            config.dashboard_oidc,
            dashboard_host=config.dashboard_host,
            trusted_hosts=config.trusted_dashboard_hosts,
        )
