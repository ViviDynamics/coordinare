"""Config checks the daemon runs before it starts, shared with every save path (225, 252).

Boot (``__main__``) and each write path (the global and section saves, the catalog and symphony
writes, and through them the config assistant and MCP) run these checks, so no write can leave a
config the daemon cannot start with.

Placeholders (252). A ``${NAME}`` is refused when NAME is not set, and a secret whose NAME is set
but empty gets a message naming it. The fields are found by type, from the config model's annotations:

* ``secret``: every ``SecretStr``, at any depth (nested models, lists, tuples, sets, dicts, unions).
* ``env``: every ``dict[str, str]`` value (performer ``env``, role ``workflow_env``). An unset
  placeholder is refused under a secret-looking key (``env_placeholders.ENV_SECRET_KEY``). Under any
  other key it is a warning, and the entry is left out of the launch (``env_placeholders``).
* ``url``: every string or URL field whose name ends in ``url`` or ``endpoint``, or that is a URL type.
* ``text``: every other string field. Its value is refused only when the whole value is a URL with a
  scheme and a host, because that is what a URL carrying a credential looks like.
* Not checked: the text fields in :data:`TEXT_FIELD_NAMES` and :data:`TEXT_FIELD_SUFFIXES`, which
  may legitimately hold ``${...}``.

The save and boot checks run on the text as written, before expansion. Only that form can tell an
unset variable from a value a set variable supplies that happens to contain braces.
"""

from __future__ import annotations

import os
import re
import types
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin
from urllib.parse import urlparse

import yaml
from pydantic import AnyUrl, BaseModel, RootModel, Secret, SecretStr, ValidationError

from coordinare.auth import validate_auth_config
from coordinare.config import ProjectConfiguration
from coordinare.dashboard_auth import validate_dashboard_bind
from coordinare.dashboard_oidc import validate_dashboard_oidc
from coordinare.env_placeholders import is_secret_env_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from coordinare.config import CoordinareConfiguration

SecretPath = tuple[str, ...]

#: Path segments for the items of a list, tuple or set, and for the values of a dict.
_LIST_ITEM = "[]"
_DICT_VALUE = "{}"
_MARKERS = frozenset({_LIST_ITEM, _DICT_VALUE})

#: The only placeholder shape expansion needs to be refused for: a braced identifier. A bare
#: ``$NAME`` is expanded too when NAME is set, and a value that merely contains ``$`` is never refused.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_URL_NAME = re.compile(r"(url|endpoint)$")

#: Text fields that may legitimately hold ``${...}``, with the reason. They are never refused.
TEXT_FIELD_NAMES: dict[str, str] = {
    "instructions": "persona prompt text, sent to the model as written",
    "prompt_addon": "persona prompt text added per scope tier",
    "criteria": "free-text curation criteria",
    "agent_command": "a shell command; the shell expands $VAR when it runs",
    "error_pattern": "a regular expression, where $ is an anchor",
}
#: Suffixes of text fields that may legitimately hold ``${...}``, with the reason.
TEXT_FIELD_SUFFIXES: dict[str, str] = {
    "_template": "a message or comment format string rendered with {placeholders}",
}


class StartCheckError(ValueError):
    """A start-time refusal, carrying the dotted field path it concerns (252)."""

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


def _is_url_type(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, AnyUrl)


def _annotation_leaves(annotation: object, stack: frozenset[type]) -> Iterator[tuple[SecretPath, object]]:
    """Yield ``(path, leaf)`` for each leaf type reachable from ``annotation``.

    A leaf is ``SecretStr``, ``str`` or a URL type. ``stack`` holds the models being entered, so a
    recursive model is cut the second time it is entered.
    """
    if annotation is SecretStr or annotation is str or _is_url_type(annotation):
        yield (), annotation
        return
    origin = get_origin(annotation)
    if origin in (Annotated, Secret):
        yield from _annotation_leaves(get_args(annotation)[0], stack)
    elif origin is dict:
        yield from _under(_DICT_VALUE, _annotation_leaves(get_args(annotation)[1], stack))
    elif origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            yield from _annotation_leaves(arg, stack)
    elif origin in (list, tuple, set, frozenset):
        for arg in get_args(annotation):
            yield from _under(_LIST_ITEM, _annotation_leaves(arg, stack))
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation not in stack:
        yield from _model_leaves(annotation, stack | {annotation})


def _under(marker: str, leaves: Iterator[tuple[SecretPath, object]]) -> Iterator[tuple[SecretPath, object]]:
    for path, leaf in leaves:
        yield (marker, *path), leaf


def _model_leaves(model: type[BaseModel], stack: frozenset[type]) -> Iterator[tuple[SecretPath, object]]:
    if issubclass(model, RootModel):
        yield from _annotation_leaves(model.model_fields["root"].annotation, stack)
        return
    for name, info in model.model_fields.items():
        for path, leaf in _annotation_leaves(info.annotation, stack):
            yield (name, *path), leaf


@cache
def _leaves(model: type[BaseModel]) -> tuple[tuple[SecretPath, object], ...]:
    return tuple(_annotation_leaves(model, frozenset()))


@cache
def secret_paths(model: type[BaseModel]) -> tuple[SecretPath, ...]:
    """Every path from ``model`` to a ``SecretStr``, found by walking the field annotations (252)."""
    return tuple(path for path, leaf in _leaves(model) if leaf is SecretStr)


def _name_of(path: SecretPath) -> str:
    return next(segment for segment in reversed(path) if segment not in _MARKERS)


def _rule(path: SecretPath, leaf: object) -> str | None:
    """The rule a leaf is checked by, or None when it is not checked (252)."""
    if leaf is SecretStr:
        return "secret"
    if path and path[-1] == _DICT_VALUE:
        return "env" if leaf is str else None
    name = _name_of(path)
    if _URL_NAME.search(name) or _is_url_type(leaf):
        return "url"
    if name in TEXT_FIELD_NAMES or any(name.endswith(suffix) for suffix in TEXT_FIELD_SUFFIXES):
        return None
    return "text" if leaf is str else None


@cache
def checked_fields(model: type[BaseModel]) -> tuple[tuple[SecretPath, str], ...]:
    """Every ``(path, rule)`` the placeholder check applies to, for ``model`` (252)."""
    checked: list[tuple[SecretPath, str]] = []
    for path, leaf in _leaves(model):
        rule = _rule(path, leaf)
        if rule is not None:
            checked.append((path, rule))
    return tuple(checked)


def _unset_refusal(field: str, name: str) -> str:
    return (
        f"{field} still holds ${{{name}}}: the environment variable {name} is not set. "
        f"Set {name} in the daemon's environment, or write the value in config.yaml."
    )


def _unset_omission(field: str, name: str) -> str:
    return (
        f"{field} uses ${{{name}}}, which is not set. The entry is left out of the performer's "
        f"environment; set {name} to pass it."
    )


def _empty_refusal(field: str, name: str) -> str:
    return (
        f"{field} uses ${{{name}}}, but the environment variable {name} is set but empty. "
        f"Set {name} to the value, or write the value in config.yaml."
    )


def _is_whole_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


@dataclass(frozen=True)
class PlaceholderFinding:
    """One placeholder problem: a refusal (boot and save stop) or a warning (the entry is left out)."""

    field: str
    message: str
    refuses: bool


def _leaf_findings(field: str, value: object, rule: str, key: str) -> list[PlaceholderFinding]:
    raw = value.get_secret_value() if isinstance(value, (SecretStr, Secret)) else value
    if not isinstance(raw, str):
        return []
    if rule == "text" and not _is_whole_url(raw):
        return []
    findings: list[PlaceholderFinding] = []
    for match in _ENV_REFERENCE.finditer(raw):
        name = match.group(1)
        if name not in os.environ:
            # An env entry under a plain key is left out of the launch, so it is a warning (252). Every
            # other unset placeholder, a secret or a URL, is refused.
            if rule == "env" and not is_secret_env_key(key):
                findings.append(PlaceholderFinding(field, _unset_omission(field, name), refuses=False))
            else:
                findings.append(PlaceholderFinding(field, _unset_refusal(field, name), refuses=True))
        elif rule == "secret" and os.environ[name] == "":
            # A secret that is empty means unset (225), so an empty variable gets its own message. A
            # blank URL or env value is a legitimate value and is not refused.
            findings.append(PlaceholderFinding(field, _empty_refusal(field, name), refuses=True))
    return findings


def _values_at(
    value: object, path: SecretPath, display: str, key: str = "",
) -> Iterator[tuple[str, object, str]]:
    """Yield ``(display path, value, dict key)`` for every value at ``path`` below ``value``."""
    if isinstance(value, Secret):
        value = value.get_secret_value()
    if not path:
        yield display, value, key
        return
    segment, rest = path[0], path[1:]
    if segment == _LIST_ITEM:
        if isinstance(value, (list, tuple, set, frozenset)):
            for index, item in enumerate(value):
                yield from _values_at(item, rest, f"{display}[{index}]", key)
        return
    if segment == _DICT_VALUE:
        if isinstance(value, dict):
            for dict_key, item in value.items():
                yield from _values_at(item, rest, f"{display}.{dict_key}", str(dict_key))
        return
    if isinstance(value, dict):
        child: object = value.get(segment)
    elif isinstance(value, BaseModel):
        child = getattr(value, segment, None)
    else:
        return
    yield from _values_at(child, rest, f"{display}.{segment}" if display else segment, key)


def placeholder_findings(
    model: type[BaseModel], value: object, prefix: str = "",
) -> list[PlaceholderFinding]:
    """Every placeholder problem in ``value``, refusals and warnings (252).

    ``value`` is a loaded model, or a dict of ``model``'s shape as written (a partial one is fine).
    ``prefix`` names where ``value`` sits, so a symphony override is reported at its own path.
    """
    findings: list[PlaceholderFinding] = []
    for path, rule in checked_fields(model):
        for display, leaf, key in _values_at(value, path, prefix):
            findings += _leaf_findings(display, leaf, rule, key)
    return findings


def placeholder_problems(
    model: type[BaseModel], value: object, prefix: str = "",
) -> list[tuple[str, str]]:
    """The ``(field path, refusal)`` pairs for the refused placeholders in ``value`` (252)."""
    return [(f.field, f.message) for f in placeholder_findings(model, value, prefix) if f.refuses]


def placeholder_warnings(
    model: type[BaseModel], value: object, prefix: str = "",
) -> list[tuple[str, str]]:
    """The ``(field path, warning)`` pairs for placeholders that are left out, not refused (252)."""
    return [(f.field, f.message) for f in placeholder_findings(model, value, prefix) if not f.refuses]


def raw_placeholder_findings(daemon_view: dict[str, Any]) -> list[PlaceholderFinding]:
    """Every placeholder problem in a config as written, before ``${VAR}`` expansion (252).

    The on-disk shape is flat (the global fields at the root, next to ``symphonies``), so the root
    is checked as ``ProjectConfiguration`` and each symphony's overrides as its partial.
    """
    findings = placeholder_findings(ProjectConfiguration, daemon_view)
    symphonies = daemon_view.get("symphonies")
    if isinstance(symphonies, list):
        for index, symphony in enumerate(symphonies):
            overrides = symphony.get("overrides") if isinstance(symphony, dict) else None
            findings += placeholder_findings(ProjectConfiguration, overrides or {}, f"symphonies[{index}].overrides")
    return findings


def raw_placeholder_problems(daemon_view: dict[str, Any]) -> list[tuple[str, str]]:
    """The refusals for a config as written (boot and save stop on these) (252)."""
    return [(f.field, f.message) for f in raw_placeholder_findings(daemon_view) if f.refuses]


def raw_placeholder_warnings(daemon_view: dict[str, Any]) -> list[tuple[str, str]]:
    """The warnings for a config as written: placeholders left out of the launch (252)."""
    return [(f.field, f.message) for f in raw_placeholder_findings(daemon_view) if not f.refuses]


def _validate_symphony_personas(
    config: ProjectConfiguration, coordinare_config: CoordinareConfiguration,
) -> None:
    """Refuse invalid symphony personas on startup and every config save path."""
    for index, symphony in enumerate(coordinare_config.symphonies):
        try:
            symphony.effective_personas(config.personas)
        except ValueError as exc:
            detail = str(exc)
            if isinstance(exc, ValidationError):
                detail = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_context=False)
                )
            message = f"Symphony {symphony.name!r}: {detail}"
            raise StartCheckError(message, f"symphonies[{index}].personas") from exc


def validate_start_time(
    config: ProjectConfiguration,
    coordinare_config: CoordinareConfiguration | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    """Raise :class:`StartCheckError` with the operator-facing reason for a config the daemon refuses.

    ``raw`` is the config as written, and the boot and save paths pass it. Without it, the loaded
    values are checked instead, and a set variable whose value holds braced text is then refused
    too. That fallback is for a caller with no text.
    """
    if raw is not None:
        problems = raw_placeholder_problems(raw)
    else:
        problems = placeholder_problems(ProjectConfiguration, config)
        if coordinare_config is not None:
            for index, symphony in enumerate(coordinare_config.symphonies):
                problems += placeholder_problems(
                    ProjectConfiguration, symphony.overrides or {}, f"symphonies[{index}].overrides",
                )
    if problems:
        field, message = problems[0]
        raise StartCheckError(message, field)
    if coordinare_config is not None:
        _validate_symphony_personas(config, coordinare_config)
    # getattr keeps the boot contract the #143 startup test pins: a config object that lacks the OIDC
    # fields is read as having none.
    oidc = getattr(config, "dashboard_oidc", None)
    try:
        validate_dashboard_bind(config.dashboard_host, config.dashboard_auth_token, oidc=oidc)
    except ValueError as exc:
        raise StartCheckError(str(exc), "dashboard_auth_token") from exc
    if oidc is not None:
        try:
            validate_dashboard_oidc(
                oidc,
                dashboard_host=config.dashboard_host,
                trusted_hosts=getattr(config, "trusted_dashboard_hosts", []),
            )
        except ValueError as exc:
            raise StartCheckError(str(exc), "dashboard_oidc") from exc
    try:
        validate_auth_config(config)
    except ValueError as exc:
        raise StartCheckError(str(exc), "github_private_key_path") from exc


def load_unexpanded_config(path: Path | None) -> dict[str, Any]:
    """The config file as written, before ``${VAR}`` expansion (252). Empty when there is no file or it does not parse."""
    if path is None or not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
