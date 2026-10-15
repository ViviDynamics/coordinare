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
    # getattr keeps the boot contract the #143 startup test pins: a config object that lacks the OIDC
    # fields is read as having none.
    oidc = getattr(config, "dashboard_oidc", None)
    validate_dashboard_bind(config.dashboard_host, config.dashboard_auth_token, oidc=oidc)
    if oidc is not None:
        validate_dashboard_oidc(
            oidc,
            dashboard_host=config.dashboard_host,
            trusted_hosts=getattr(config, "trusted_dashboard_hosts", []),
        )
