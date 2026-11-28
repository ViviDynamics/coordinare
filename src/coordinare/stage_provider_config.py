"""402: configuration of stage providers, the outside services that can fill a stage.

A provider is declared once in the root ``stage_providers`` catalog and chosen per role
with ``performers.<role>.provider``. A symphony opts in through its ``overrides``; a
config that names no provider runs every stage on performers, exactly as before.

Secrets are never written here: ``token_env`` names the environment variable that holds
the token, and the value is read when a request is made.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Roles each provider kind can fill; the stage contract itself is shared.
PROVIDER_ROLES: dict[str, frozenset[str]] = {
    "scrutare": frozenset({"reviewer"}),
    # 404: QAre fills the QA stage.
    "qare": frozenset({"qa"}),
    # 403: Progettare fills the architecting stage.
    "progettare": frozenset({"architect"}),
}

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _image_or_none(value: str | None) -> str | None:
    """423: a performer image reference, or None for the role's performer image."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if any(c.isspace() for c in value) or value.startswith("-"):
        msg = "image must be one image reference, for example ghcr.io/vividynamics/coordinare-performer-full:2026.11.0"
        raise ValueError(msg)
    return value
_PROVIDER_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


#: A released version, as ``progettare --version`` prints it (``2026.11.3``, ``1.2.0``).
_RELEASE = re.compile(r"^\d+(\.\d+){1,3}([.+-][0-9A-Za-z.]+)?$")


def _env_name(value: str, field: str) -> str:
    if not _ENV_NAME.match(value or ""):
        msg = (
            f"{field} must be the NAME of an environment variable (for example "
            "GITHUB_TOKEN), never its value"
        )
        raise ValueError(msg)
    return value


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
    #: ``vivi-scrutare[bot]``. Required: its reviews are the stage's own output, and
    #: monitor_pr can only keep them from being routed as outside feedback, or counted
    #: as an approval, when it knows whose they are.
    review_login: str
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            msg = "url must be an http(s) URL with a host, for example http://scrutare:8080"
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
    def _validate_review_login(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            msg = "review_login must name the GitHub login Scrutare posts its reviews under"
            raise ValueError(msg)
        return value


#: 442: variables that would move QAre's browser cache away from what the image links in.
_QARE_BROWSER_CACHE_ENV = frozenset({"XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"})
_QARE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
#: 435: variables that point a Docker, BuildKit or Podman client at a daemon. A sandboxed run
#: must talk only to its own engine, so none of them may be passed through.
_DOCKER_CLIENT_ENV = re.compile(r"^(DOCKER_|BUILDKIT_|BUILDX_|COMPOSE_|CONTAINER_HOST$|CONTAINERD_)")


class QAreProviderConfig(BaseModel):
    """404: QAre for the QA stage, run as its CLI (``qare plan``, ``qare run``).

    QAre has no service to call: each run is a stage job (#423), an ephemeral performer
    container that checks the pull request out and runs the pinned CLI there, with QAre's
    posting turned off; coordinare posts the evidence itself. QAre needs no token from
    coordinare; the model settings its nare reads reach it only through
    ``env_passthrough``, by variable name.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal["qare"]
    #: The exact version ``qare --version`` must print. QAre's interfaces can change
    #: between releases, so a run on any other version is refused.
    version: str
    #: The command that starts QAre in the performer, as an argument list (``["qare"]``,
    #: or a path inside the performer image).
    command: list[str] = Field(default_factory=lambda: ["qare"])
    #: 423: the performer image runs use. None (default) is the image of the performer
    #: endpoint that serves the ``qa`` role, else of the first one with an image.
    image: str | None = None
    #: 435: ``sysbox`` gives each run its own Docker engine, for profiles that boot the app
    #: with ``docker compose``: the run's container starts under the Sysbox runtime
    #: (``--runtime=sysbox-runc``, a Linux host prerequisite doctor checks) from an image
    #: with a Docker engine (``Dockerfile.sandbox``). Never privileged, never the host's
    #: Docker. None (default) runs without Docker, exactly as before.
    sandbox: Literal["sysbox"] | None = None
    #: The QA profile's directory in the repository under test.
    profile: str = ".qa"
    #: Where runs keep their request, status and evidence (never a checkout: that stays in
    #: the performer). Mounted into each run's container at the same path. Defaults to a
    #: directory beside the state file, so a restarted daemon finds its runs again.
    work_dir: str | None = None
    #: NAMES of environment variables QAre (and the nare it runs) may read, for example
    #: its model endpoint and key. Nothing else from coordinare's environment reaches it.
    env_passthrough: list[str] = Field(default_factory=list)
    #: How long one run (checkout, plan and run) may take before it is stopped.
    timeout_seconds: int = Field(default=3600, gt=0, le=86400)
    #: Days a finished run's directory (its evidence and logs) is kept. Its checkouts go
    #: with its performer as soon as it ends; the evidence is on ``qa-assets`` by then.
    retain_days: int = Field(default=14, ge=1, le=3650)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        value = (value or "").strip()
        if not _QARE_VERSION.match(value):
            msg = "version must be the exact QAre release to run, for example 2026.11.42"
            raise ValueError(msg)
        return value

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str | None) -> str | None:
        return _image_or_none(value)

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: list[str]) -> list[str]:
        if not value or any(not str(part).strip() for part in value):
            msg = "command must be a non-empty list of arguments, for example [\"qare\"]"
            raise ValueError(msg)
        return [str(part) for part in value]

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: str) -> str:
        raw = (value or "").strip()
        path = PurePosixPath(raw)
        if not raw or not path.parts or path.is_absolute() or ".." in path.parts or "\\" in raw:
            msg = "profile must be a relative path inside the repository, for example .qa"
            raise ValueError(msg)
        return str(path)

    @field_validator("env_passthrough")
    @classmethod
    def _validate_env_passthrough(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _ENV_NAME.match(name or ""):
                msg = (
                    "env_passthrough lists environment variable NAMES (for example "
                    "OPENAI_API_KEY), never their values"
                )
                raise ValueError(msg)
            if name in _QARE_BROWSER_CACHE_ENV:
                msg = (
                    f"env_passthrough may not name {name}: it moves where QAre's Playwright "
                    "looks for the browser the performer image ships (#442)"
                )
                raise ValueError(msg)
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _sandbox_talks_only_to_its_own_engine(self) -> QAreProviderConfig:
        if self.sandbox is not None:
            clients = [name for name in self.env_passthrough if _DOCKER_CLIENT_ENV.match(name)]
            if clients:
                msg = (
                    f"env_passthrough must not name {', '.join(clients)} with a sandbox: a Docker client "
                    "variable could point the run at another daemon instead of the sandbox's own engine"
                )
                raise ValueError(msg)
        return self


class ProgettareProviderConfig(BaseModel):
    """403: Progettare's released CLI, run by coordinare for the architecting stage.

    Progettare has no job API of its own: each request is a stage job (#423), an ephemeral
    performer container that clones the default branch and runs ``progettare blueprint``,
    with one run directory per request id under ``runs_dir`` mounted into it; coordinare
    reads the run's artifacts. It pins its own harness and models (``progettare.yaml``), so the
    architect's ``backend``, ``mode`` and ``workflow`` have no effect while it is set.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal["progettare"]
    #: The CLI to run in the performer: a name on its PATH or an absolute path.
    command: str = "progettare"
    #: 423: the performer image runs use. None (default) is the image of the performer
    #: endpoint that serves the ``architect`` role, else of the first one with an image.
    image: str | None = None
    #: The release coordinare speaks to. ``progettare --version`` must print exactly
    #: this; doctor and the startup preflight check it, and a run refuses to start
    #: under another version.
    version: str
    #: Where run directories are kept, one per request id, each mounted into its run's
    #: container at the same path. Must survive a restart of coordinare: it is how a
    #: restarted daemon finds a run already in flight.
    runs_dir: str
    #: ``progettare.yaml`` to plan with. None (default) uses the one on the planned
    #: repository's default branch.
    config_path: str | None = None
    #: Name of the environment variable holding a GitHub token that can read the issue
    #: and clone the repository. Handed to the run as ``GH_TOKEN``.
    gh_token_env: str
    #: Names of further environment variables the run receives, for example the model
    #: provider's credential (``OPENAI_API_KEY``). Nothing else is passed through.
    pass_env: list[str] = Field(default_factory=list)
    #: A run that takes longer is stopped and retried as a system error.
    timeout_seconds: int = Field(default=3600, gt=0, le=6 * 3600)

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str | None) -> str | None:
        return _image_or_none(value)

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        value = (value or "").strip()
        relative_path = "/" in value and not PurePosixPath(value).is_absolute()
        if not value or any(c.isspace() for c in value) or relative_path:
            msg = "command must be the progettare executable: a name on PATH or an absolute path, no arguments"
            raise ValueError(msg)
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        value = (value or "").strip()
        if not _RELEASE.match(value):
            msg = "version must be a release, as `progettare --version` prints it (for example 2026.11.3)"
            raise ValueError(msg)
        return value

    @field_validator("runs_dir", "config_path")
    @classmethod
    def _validate_absolute(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field = getattr(info, "field_name", "path")
        if not PurePosixPath(value.strip()).is_absolute():
            msg = f"{field} must be an absolute path on the coordinare host"
            raise ValueError(msg)
        return value.strip().rstrip("/") or "/"

    @field_validator("gh_token_env")
    @classmethod
    def _validate_gh_token_env(cls, value: str) -> str:
        return _env_name(value, "gh_token_env")

    @field_validator("pass_env")
    @classmethod
    def _validate_pass_env(cls, value: list[str]) -> list[str]:
        return [_env_name(name, "every pass_env entry") for name in value]


#: One entry of the ``stage_providers`` catalog, chosen by its ``kind``.
StageProviderConfig = Annotated[
    ScrutareProviderConfig | QAreProviderConfig | ProgettareProviderConfig, Field(discriminator="kind"),
]


def normalize_login(login: str) -> str:
    """A GitHub login as both APIs can be compared on.

    REST names an App's bot ``slug[bot]``; GraphQL's ``author.login`` names it ``slug``.
    """
    value = (login or "").strip().lower()
    return value.removesuffix("[bot]")


def validate_provider_name(name: str) -> None:
    if not _PROVIDER_NAME.match(name):
        msg = f"stage_providers key {name!r} must be lower-case letters, digits and dashes"
        raise ValueError(msg)


def provider_role_errors(
    providers: dict[str, StageProviderConfig],
    role_providers: dict[str, str],
    human_reviewers: list[str],
) -> list[str]:
    """Every reason the catalog and the roles that reference it do not agree."""
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
    humans = {normalize_login(login) for login in human_reviewers}
    for name, provider in sorted(providers.items()):
        review_login = getattr(provider, "review_login", None)
        if review_login and normalize_login(review_login) in humans:
            errors.append(
                f"stage_providers.{name}.review_login {review_login!r} is also listed in "
                "human_reviewers; a provider's approval must never count as a human approval",
            )
    return errors
