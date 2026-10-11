"""Bearer auth for incoming MCP requests (OAuth resource server in phase 3)."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import SecretStr


def credential_fingerprint(token: str) -> str:
    """Return the first 8 hex of sha256(token); never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def check_bearer(auth_header: str | None, expected: SecretStr) -> bool:
    """Constant-time bearer check accepting only the ``Authorization: Bearer`` scheme.

    Mirrors the compare pattern in ``dashboard_auth.py``.
    """
    if not auth_header:
        return False
    scheme, _, value = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.encode(), expected.get_secret_value().encode())


__all__ = ["check_bearer", "credential_fingerprint"]
