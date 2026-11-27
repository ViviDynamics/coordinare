"""402: configuration of stage providers, the outside services that can fill a stage.

A provider is declared once in the root ``stage_providers`` catalog and chosen per role
with ``performers.<role>.provider``. A symphony opts in through its ``overrides``; a
config that names no provider runs every stage on performers, exactly as before.

Secrets are never written here: ``token_env`` names the environment variable that holds
the token, and the value is read when a request is made.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Roles each provider kind can fill. #403 (Progettare, architect) and #404 (QAre, qa)
#: add their kinds here; the stage contract itself is shared.
PROVIDER_ROLES: dict[str, frozenset[str]] = {
    "scrutare": frozenset({"reviewer"}),
}

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class ScrutareProviderConfig(BaseModel):
    """Scrutare's performer API (``POST /api/reviews``, ``GET /api/jobs/<id>``)."""

    # The token reference must never echo a secret back in a validation error, even
    # when someone pastes the token itself where its variable name belongs.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal["scrutare"]
    #: Base URL of the Scrutare App service, as reachable from coordinare (the cluster
    #: Service, not the public webhook route).
    url: str
    #: Name of the environment variable holding Scrutare's performer API token
    #: (``SCRUTARE_APP_API_TOKEN`` on the Scrutare side).
    token_env: str
    #: The GitHub App installation id Scrutare is allowed to use for the repository.
    installation: int = Field(gt=0)
    #: The GitHub login Scrutare's reviews are posted under, for example
    #: ``vivi-scrutare[bot]``. Its reviews are the stage's own output: monitor_pr never
    #: routes them as outside feedback and never counts them as a human approval.
    review_login: str | None = None
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            msg = "url must be an http(s) URL with a host, for example http://scrutare.scrutare.svc.cluster.local"
            raise ValueError(msg)
        if parts.username or parts.password or parts.query or parts.fragment:
            msg = "url must not carry credentials, a query or a fragment; the token comes from token_env"
            raise ValueError(msg)
        return value.strip().rstrip("/")

    @field_validator("token_env")
    @classmethod
    def _validate_token_env(cls, value: str) -> str:
        if not _ENV_NAME.match(value or ""):
            msg = (
                "token_env must be the NAME of an environment variable (for example "
                "SCRUTARE_API_TOKEN), never the token itself"
            )
            raise ValueError(msg)
        return value

    @field_validator("review_login")
    @classmethod
    def _validate_review_login(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


#: One entry of the ``stage_providers`` catalog. A union once a second kind exists.
StageProviderConfig = ScrutareProviderConfig


def validate_provider_name(name: str) -> None:
    if not _PROVIDER_NAME.match(name):
        msg = f"stage_providers key {name!r} must be lower-case letters, digits and dashes"
        raise ValueError(msg)


def provider_role_errors(
    providers: dict[str, StageProviderConfig],
    role_providers: dict[str, str],
    human_reviewers: list[str],
) -> list[str]:
    """Every reason the catalog and the roles that reference it do not fit together."""
    errors: list[str] = []
    for name in providers:
        try:
            validate_provider_name(name)
        except ValueError as exc:
            errors.append(str(exc))
    for role, name in sorted(role_providers.items()):
        provider = providers.get(name)
        if provider is None:
            known = ", ".join(sorted(providers)) or "none are declared"
            errors.append(
                f"performers.{role}.provider names {name!r}, which is not in stage_providers ({known})",
            )
            continue
        allowed = PROVIDER_ROLES.get(provider.kind, frozenset())
        if role not in allowed:
            errors.append(
                f"performers.{role}.provider: a {provider.kind} provider can fill "
                f"{', '.join(sorted(allowed)) or 'no role'}, not {role}",
            )
    humans = {login.strip().lower() for login in human_reviewers}
    for name, provider in sorted(providers.items()):
        if provider.review_login and provider.review_login.lower() in humans:
            errors.append(
                f"stage_providers.{name}.review_login {provider.review_login!r} is also listed in "
                "human_reviewers; a provider's approval must never count as a human approval",
            )
    return errors
