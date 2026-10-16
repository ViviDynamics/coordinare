"""Preview and selectively adopt the current chart seed without losing edits."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from coordinare.config_descriptors import SECRET_MASK
from coordinare.env_placeholders import is_secret_env_key
from coordinare.services.config_ownership import chart_owned_keys
from coordinare.services.config_secret_policy import credential_url_password, persistent_fields
from coordinare.services.config_seed import (
    _changed,
    _read_document,
    _sections,
    acknowledge_sections,
)
from coordinare.services.config_write_service import (
    ConcurrencyConflictError,
    _validate_candidate,
    atomic_write_yaml,
    classify_section_applied,
    compute_content_hash,
    guard_concurrency,
)
from coordinare.start_checks import _DICT_VALUE, _LIST_ITEM

if TYPE_CHECKING:
    from pathlib import Path


class AdoptionRefusalError(Exception):
    def __init__(self, message: str, status: HTTPStatus) -> None:
        super().__init__(message)
        self.status = status


def _mask_at(value: Any, path: tuple[str, ...], rule: str, key: str = "") -> Any:
    """Mask model declared credentials without importing dashboard routers."""
    if not path:
        secret = rule == "secret" or is_secret_env_key(key)
        if secret and isinstance(value, dict):
            return {name: _mask_at(item, (), rule, str(name)) for name, item in value.items()}
        return SECRET_MASK if secret and value not in (None, "") else value
    head, tail = path[0], path[1:]
    if head == _LIST_ITEM and isinstance(value, list):
        return [_mask_at(item, tail, rule, key) for item in value]
    if head == _DICT_VALUE and isinstance(value, dict):
        return {name: _mask_at(item, tail, rule, str(name)) for name, item in value.items()}
    if isinstance(value, dict) and head in value:
        return {**value, head: _mask_at(value[head], tail, rule, head)}
    return value


def _mask_url_credentials(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_url_credentials(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_url_credentials(item) for item in value]
    if isinstance(value, str) and credential_url_password(value) is not None:
        return SECRET_MASK
    return value


def _masked(raw: dict[str, Any]) -> dict[str, Any]:
    masked = dict(raw)
    for path, rule in persistent_fields():
        if rule in {"secret", "env"}:
            masked = _mask_at(masked, path, rule)
    symphonies = masked.get("symphonies")
    if isinstance(symphonies, list):
        masked["symphonies"] = [
            {**symphony, "overrides": _masked(symphony["overrides"])}
            if isinstance(symphony, dict) and isinstance(symphony.get("overrides"), dict)
            else symphony
            for symphony in symphonies
        ]
    return dict(_mask_url_credentials(masked))


def chart_preview(seed: Path, live: Path) -> dict[str, Any]:
    chart, seed_hash = _read_document(seed)
    current, content_hash = _read_document(live)
    before, after = _masked(current), _masked(chart)
    sections = _changed(_sections(current, chart), _sections(chart))
    return {
        "seed_hash": seed_hash,
        "content_hash": content_hash,
        "sections": [
            {
                "name": key,
                "live": before.get(key),
                "chart": after.get(key),
                "removed": key not in chart,
            }
            for key in sections
        ],
    }


def _selection(body: dict[str, Any], available: set[str]) -> list[str]:
    selected = body.get("sections")
    if (
        body.get("confirmed") is not True
        or not isinstance(selected, list)
        or not selected
        or any(not isinstance(key, str) for key in selected)
    ):
        message = "Confirm adoption and select at least one section."
        raise AdoptionRefusalError(message, HTTPStatus.UNPROCESSABLE_ENTITY)
    if set(selected) - available:
        message = "Select only differing sections that are not managed by the Helm chart."
        raise AdoptionRefusalError(message, HTTPStatus.UNPROCESSABLE_ENTITY)
    return list(dict.fromkeys(selected))


def adopt_chart_sections(
    seed: Path, live: Path, body: dict[str, Any], expected_hash: str,
) -> dict[str, Any]:
    """Validate and guard both documents immediately before the atomic swap."""
    chart, seed_hash = _read_document(seed)
    current, current_hash = _read_document(live)
    if current_hash != expected_hash.removeprefix("W/").strip('"'):
        message = "Live config changed since preview; review the diff again."
        raise AdoptionRefusalError(message, HTTPStatus.CONFLICT)
    available = set(_changed(_sections(current, chart), _sections(chart))) - chart_owned_keys(chart)
    selected = _selection(body, available)
    if body.get("seed_hash") != seed_hash:
        message = "Chart seed changed since preview; review the diff again."
        raise AdoptionRefusalError(message, HTTPStatus.CONFLICT)
    candidate = dict(current)
    for key in selected:
        if key in chart:
            candidate[key] = chart[key]
        else:
            candidate.pop(key, None)
    errors = _validate_candidate(candidate, original=current)
    if errors:
        message = "; ".join(error.message for error in errors)
        raise AdoptionRefusalError(message, HTTPStatus.UNPROCESSABLE_ENTITY)
    try:
        guard_concurrency(live, expected_hash.removeprefix("W/").strip('"'))
    except ConcurrencyConflictError as exc:
        raise AdoptionRefusalError(str(exc), HTTPStatus.CONFLICT) from None
    if compute_content_hash(seed) != seed_hash:
        message = "Chart seed changed during validation; review the diff again."
        raise AdoptionRefusalError(message, HTTPStatus.CONFLICT)
    new_hash = atomic_write_yaml(live, candidate)
    result = {
        "ok": True,
        "new_hash": new_hash,
        "applied": classify_section_applied("global", selected),
    }
    try:
        acknowledge_sections(live, selected, chart, seed_hash)
    except OSError:
        result["message"] = (
            "Config saved; provenance could not be updated. Review remaining drift before retrying."
        )
    return result
