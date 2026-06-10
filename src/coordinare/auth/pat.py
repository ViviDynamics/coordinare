"""PatAuth — GitHub authentication backed by a static personal access token."""
from __future__ import annotations


class PatAuth:
    """GitHubAuth backed by a static personal access token.

    Raises ValueError at construction if token is empty.
    """

    def __init__(self, token: str) -> None:
        if not token or not token.strip():
            msg = "PatAuth: token must be non-empty"
            raise ValueError(msg)
        self._token = token

    async def get_token(self) -> str:
        return self._token

    async def invalidate(self) -> None:
        """No-op: a static PAT has no cached credential to discard.

        Returning without changing state means the next ``get_token()``
        yields the same token — which is how the service detects a
        credential that cannot be refreshed and fails fast.
        """
        return None
