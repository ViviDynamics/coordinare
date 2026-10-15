"""Secret overrides on a symphony (#221): masked on the way out, kept on the way back in.

A secret at any depth of a symphony's overrides is served as the mask, so the browser never holds
its value. A masked secret sent back keeps the value the server stores. Paths come from the #252
walker over the global model, so nothing here is a hand-kept list.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from coordinare.config import ProjectConfiguration
from coordinare.config_descriptors import SECRET_MASK, _global_field_required
from coordinare.start_checks import _DICT_VALUE, _LIST_ITEM, secret_paths

# 221: every secret the global model declares, as a path from the top of an override. The #252
# walker finds each SecretStr leaf, nested ones included (a secret inside a dict-valued field), so
# the paths come from the model and no list is kept by hand.
_SECRET_PATHS = secret_paths(ProjectConfiguration)
# The top-level override keys that hold a secret directly. The page edits these as a field of their own.
_TOP_LEVEL_SECRET_KEYS = frozenset(path[0] for path in _SECRET_PATHS if len(path) == 1)
_MISSING: Any = object()


class NoStoredSecretError(Exception):
    """A masked secret was sent back, and the server holds no value for it to keep."""


def _walk(value: Any, stored: Any, rest: tuple[str, ...], leaf: Any) -> Any:
    """Apply ``leaf(value, stored)`` at every place ``rest`` reaches inside ``value``.

    ``stored`` is the matching value the server holds, or ``_MISSING``. A dict-valued field pairs
    entries by key, a list pairs items by index, and a named field pairs by name.
    """
    if not rest:
        return leaf(value, stored)
    head, tail = rest[0], rest[1:]
    stored_map: dict[str, Any] = stored if isinstance(stored, dict) else {}
    stored_list: list[Any] = stored if isinstance(stored, list) else []
    if head == _DICT_VALUE and isinstance(value, dict):
        return {key: _walk(item, stored_map.get(key, _MISSING), tail, leaf) for key, item in value.items()}
    if head == _LIST_ITEM and isinstance(value, list):
        return [
            _walk(item, stored_list[index] if index < len(stored_list) else _MISSING, tail, leaf)
            for index, item in enumerate(value)
        ]
    if head not in (_DICT_VALUE, _LIST_ITEM) and isinstance(value, dict) and head in value:
        return {**value, head: _walk(value[head], stored_map.get(head, _MISSING), tail, leaf)}
    return value


def _mask_leaf(value: Any, _stored: Any) -> Any:
    return SECRET_MASK if value not in (None, "") else value


def _keep_leaf(value: Any, stored: Any) -> Any:
    """A masked secret keeps the value the server holds. The mask itself is never written."""
    if value != SECRET_MASK:
        return value
    if stored is _MISSING:
        raise NoStoredSecretError
    return stored


def _mask_secret_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """221: the overrides as the browser may hold them: every secret at any depth reads as the mask."""
    masked = dict(overrides)
    for path in _SECRET_PATHS:
        if path[0] in masked:
            masked[path[0]] = _walk(masked[path[0]], _MISSING, path[1:], _mask_leaf)
    return masked


def _restore_masked_secrets(proposed: Any, stored: dict[str, Any]) -> tuple[Any, JSONResponse | None]:
    """221: a secret the page sent back still masked keeps its stored value, at whatever depth it sits.

    A masked secret with nothing stored to keep is refused, because the mask is not a value.
    """
    if not isinstance(proposed, dict):
        return proposed, None
    restored = dict(proposed)
    key = ""
    try:
        for path in _SECRET_PATHS:
            key = path[0]
            if key in restored:
                restored[key] = _walk(restored[key], stored.get(key, _MISSING), path[1:], _keep_leaf)
    except NoStoredSecretError:
        return proposed, JSONResponse(
            {"error": f"{key}: there is no stored secret to keep, so type the new value"},
            status_code=422,
        )
    return restored, None


def _secret_override_fields(daemon: Any, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """221: the top-level secret overrides the page edits, each with whether it may be cleared.

    A secret is clearable only where #225 lets the global field be empty: the same required rule.
    """
    global_cfg = getattr(daemon.state.get("coordinare_config"), "global_config", None)
    fields: list[dict[str, Any]] = []
    for key in sorted(overrides):
        if key not in _TOP_LEVEL_SECRET_KEYS:
            continue
        info = ProjectConfiguration.model_fields[key]
        required = info.is_required() if global_cfg is None else _global_field_required(global_cfg, key, info)
        fields.append({"key": key, "clearable": not required})
    return fields
