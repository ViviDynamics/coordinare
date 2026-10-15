"""Config checks the daemon runs before it starts, shared with every save path (225, 252).

Boot (``__main__``, ``_run``) and each write path (the global and section saves, the catalog
and symphony writes, and through them the config assistant and MCP) call
:func:`validate_start_time`, so no write can leave a config the daemon cannot start with.

The secret fields are found by type (252): every ``SecretStr`` reachable from the config model,
through nested models, lists and dicts, is checked for an unexpanded ``${VAR}``. A symphony's
``overrides`` are checked as the partial ``ProjectConfiguration`` they are merged into.
"""

from __future__ import annotations

import os
import re
import types
from functools import cache
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from pydantic import BaseModel, RootModel, SecretStr

from coordinare.auth import validate_auth_config
from coordinare.config import ProjectConfiguration
from coordinare.dashboard_auth import validate_dashboard_bind
from coordinare.dashboard_oidc import validate_dashboard_oidc

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coordinare.config import CoordinareConfiguration

SecretPath = tuple[str, ...]

#: Path segments for the items of a list and the values of a dict.
_LIST_ITEM = "[]"
_DICT_VALUE = "{}"

#: The only placeholder shape expansion acts on: a braced identifier. A bare ``$NAME`` or a
#: value that merely contains ``$`` (a password, say) is never read as a placeholder.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@cache
def secret_paths(model: type[BaseModel]) -> tuple[SecretPath, ...]:
    """Every path from ``model`` to a ``SecretStr`` field, found by walking the field annotations."""
    return tuple(_annotation_secret_paths(model, frozenset()))


def _annotation_secret_paths(annotation: object, stack: frozenset[type]) -> Iterator[SecretPath]:
    if annotation is SecretStr:
        yield ()
        return
    origin = get_origin(annotation)
    if origin is list:
        for sub in _annotation_secret_paths(get_args(annotation)[0], stack):
            yield (_LIST_ITEM, *sub)
    elif origin is dict:
        for sub in _annotation_secret_paths(get_args(annotation)[1], stack):
            yield (_DICT_VALUE, *sub)
    elif origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            yield from _annotation_secret_paths(arg, stack)
    elif isinstance(annotation, type) and issubclass(annotation, RootModel):
        yield from _annotation_secret_paths(annotation.model_fields["root"].annotation, stack)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation not in stack:
        for name, info in annotation.model_fields.items():
            for sub in _annotation_secret_paths(info.annotation, stack | {annotation}):
                yield (name, *sub)


def unexpanded_secret_problem(field: str, value: object) -> str | None:
    """The refusal for a secret that still holds an unexpanded ``${VAR}``, or None (225, 252).

    Expansion replaces ``${VAR}`` only when VAR is set, so a placeholder that survives it names an
    unset variable. A set variable whose value happens to contain braced text is not refused.
    The message names the variable and the field, never the secret.
    """
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not isinstance(raw, str):
        return None
    for match in _ENV_REFERENCE.finditer(raw):
        name = match.group(1)
        if name in os.environ:
            continue
        return (
            f"{field} still holds ${{{name}}}: the environment variable {name} is not set. "
            f"Set {name} in the daemon's environment, or write the value in config.yaml."
        )
    return None


def _values_at(value: object, path: SecretPath, display: str) -> Iterator[tuple[str, object]]:
    """Yield ``(display path, value)`` for every value at ``path`` below ``value``."""
    if not path:
        yield display, value
        return
    segment, rest = path[0], path[1:]
    if segment == _LIST_ITEM:
        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from _values_at(item, rest, f"{display}[{index}]")
        return
    if segment == _DICT_VALUE:
        if isinstance(value, dict):
            for key, item in value.items():
                yield from _values_at(item, rest, f"{display}.{key}")
        return
    if isinstance(value, dict):
        child: object = value.get(segment)
    elif isinstance(value, BaseModel):
        child = getattr(value, segment, None)
    else:
        return
    yield from _values_at(child, rest, f"{display}.{segment}" if display else segment)


def secret_problems(
    model: type[BaseModel], value: object, prefix: str = "",
) -> list[tuple[str, str]]:
    """The ``(field path, refusal)`` pairs for every unexpanded secret in ``value`` (252).

    ``value`` is a loaded model or a raw dict of ``model``'s shape (a partial one is fine).
    ``prefix`` names where ``value`` sits, so a symphony override is reported at its own path.
    """
    problems: list[tuple[str, str]] = []
    for path in secret_paths(model):
        for display, leaf in _values_at(value, path, prefix):
            message = unexpanded_secret_problem(display, leaf)
            if message is not None:
                problems.append((display, message))
    return problems


def validate_start_time(
    config: ProjectConfiguration, coordinare_config: CoordinareConfiguration | None = None,
) -> None:
    """Raise ``ValueError`` with the operator-facing reason for a config the daemon refuses at boot."""
    problems = secret_problems(ProjectConfiguration, config)
    if coordinare_config is not None:
        for index, symphony in enumerate(coordinare_config.symphonies):
            problems += secret_problems(ProjectConfiguration, symphony.overrides or {}, f"symphonies[{index}].overrides")
    if problems:
        raise ValueError(problems[0][1])
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
    """The ``(field path, refusal)`` pairs for unexpanded secrets in an expanded, unvalidated config (252).

    The on-disk shape is flat (the global fields at the root, next to ``symphonies``), so the root
    is checked as ``ProjectConfiguration`` and each symphony's overrides as its partial.
    """
    problems = secret_problems(ProjectConfiguration, daemon_view)
    symphonies = daemon_view.get("symphonies")
    if isinstance(symphonies, list):
        for index, symphony in enumerate(symphonies):
            overrides = symphony.get("overrides") if isinstance(symphony, dict) else None
            problems += secret_problems(ProjectConfiguration, overrides or {}, f"symphonies[{index}].overrides")
    return problems
