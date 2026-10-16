from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from coordinare.config import ProjectConfiguration
from coordinare.env_placeholders import is_secret_env_key
from coordinare.start_checks import _DICT_VALUE, _values_at, checked_fields

if TYPE_CHECKING:
    from collections.abc import Iterator

_ENV_IDENTIFIER = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_POINTER_SUFFIXES = ("_ENV_KEY", "_ENV", "_VAR", "_NAME")

_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_AUTH_SCHEME = re.compile(r"^(?:Bearer|Basic|Token) ", re.IGNORECASE)


def _placeholder_only(value: str) -> bool:
    credential = _AUTH_SCHEME.sub("", value, count=1)
    return bool(_PLACEHOLDER.search(credential)) and not _PLACEHOLDER.sub("", credential)


def persistent_fields() -> Iterator[tuple[tuple[str, ...], str]]:
    """Include every OTLP header value, even names without a secret keyword."""
    for path, rule in checked_fields(ProjectConfiguration):
        if path == ("observability", "otlp", "headers", _DICT_VALUE):
            path = path[:-1]
            rule = "secret"
        yield path, rule


def credential_url_password(value: str) -> str | None:
    """Return a userinfo password, or an empty string for userinfo without one."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme and parsed.netloc and parsed.username is not None:
        return parsed.password or ""
    return None


def _literal_url_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_literal_url_credentials(item) for item in value.values())
    if isinstance(value, list):
        return any(_literal_url_credentials(item) for item in value)
    if isinstance(value, str):
        password = credential_url_password(value)
        return password is not None and not _placeholder_only(password)
    return False


def _literal_credential(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_literal_credential(item) for item in value.values())
    return not (
        value is None or value == ""
        or (isinstance(value, str) and _placeholder_only(value))
    )


_PERSISTENCE_PROBLEM = (
    "Persisted config secrets must use a ${VAR} placeholder or null. "
    "Supply credentials through the Kubernetes Secret."
)


def persistent_secret_problem(raw: dict[str, Any]) -> str | None:
    """Require credential indirection in persisted config without echoing a value."""
    if _literal_url_credentials(raw):
        return _PERSISTENCE_PROBLEM
    documents = [raw]
    symphonies = raw.get("symphonies", [])
    if isinstance(symphonies, list):
        documents.extend(
            symphony["overrides"]
            for symphony in symphonies
            if isinstance(symphony, dict) and isinstance(symphony.get("overrides"), dict)
        )
    for document in documents:
        for path, rule in persistent_fields():
            if rule not in {"secret", "env"}:
                continue
            for _display, value, key in _values_at(document, path, ""):
                if rule == "env" and (
                    not is_secret_env_key(key)
                    or (
                        key.upper().endswith(_POINTER_SUFFIXES)
                        and isinstance(value, str)
                        and _ENV_IDENTIFIER.fullmatch(value)
                        and is_secret_env_key(value)
                    )
                ):
                    continue
                if _literal_credential(value):
                    return _PERSISTENCE_PROBLEM
    return None
