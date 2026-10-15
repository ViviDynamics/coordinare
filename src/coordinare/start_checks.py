"""Config checks the daemon runs before it starts, shared with every save path (225).

Boot (``__main__``, ``_run``) and each write path (the global and section saves, the catalog
and symphony writes, and through them the config assistant and MCP) call
:func:`validate_start_time`, so no write can leave a config the daemon cannot start with.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from coordinare.auth import validate_auth_config
from coordinare.dashboard_auth import validate_dashboard_bind
from coordinare.dashboard_oidc import validate_dashboard_oidc

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration

#: The secret fields the daemon reads, as paths from the config root. A secret is found by type
#: for the top-level fields; the two nested ones are the webhook signing secret and the OIDC secret.
SECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("github_token",),
    ("dashboard_auth_token",),
    ("webhooks", "secret"),
    ("dashboard_oidc", "client_secret"),
)

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def unexpanded_secret_problem(field: str, value: object) -> str | None:
    """The refusal for a secret that still holds an unexpanded ``${VAR}``, or None (225 round 2).

    An unset ``${VAR}`` stays literal after expansion. Read as a secret it is a publicly known
    value (the literal string itself), so the daemon refuses it and names the variable to set.
    """
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
    if not isinstance(raw, str):
        return None
    match = _ENV_REFERENCE.search(raw)
    if match is None:
        return None
    name = match.group(1)
    return (
        f"{field} still holds ${{{name}}}: the environment variable {name} is not set. "
        f"Set {name} in the daemon's environment, or write the value in config.yaml."
    )


def _attribute_path(config: object, path: tuple[str, ...]) -> object:
    value: object = config
    for part in path:
        value = getattr(value, part, None)
    return value


def validate_start_time(config: ProjectConfiguration) -> None:
    """Raise ``ValueError`` with the operator-facing reason for a config the daemon refuses at boot."""
    for path in SECRET_PATHS:
        problem = unexpanded_secret_problem(".".join(path), _attribute_path(config, path))
        if problem is not None:
            raise ValueError(problem)
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
    validate_auth_config(config)


def raw_secret_problems(daemon_view: dict[str, Any]) -> list[tuple[str, str]]:
    """The (field, refusal) pairs for unexpanded secrets in an expanded, unvalidated config dict."""
    problems: list[tuple[str, str]] = []
    for path in SECRET_PATHS:
        value: object = daemon_view
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        message = unexpanded_secret_problem(".".join(path), value)
        if message is not None:
            problems.append((".".join(path), message))
    return problems
