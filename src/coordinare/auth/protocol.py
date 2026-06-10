"""GitHubAuth Protocol — the shared interface for all auth implementations."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitHubAuth(Protocol):
    """Provides a current, valid GitHub bearer token.

    Both PAT and App auth implement this protocol. All consumers
    (GitHubService, performer token injection) call get_token() only —
    they never interact with credential lifecycle directly.
    """

    async def get_token(self) -> str:
        """Return a current, valid GitHub bearer token string.

        For PAT mode: returns the static token immediately.
        For App mode: returns the cached installation token, refreshing
        it transparently if it is within 5 minutes of expiry.

        Raises:
            TransientGitHubError: token acquisition failed transiently.
            ValueError: misconfigured credentials (programming error).
        """
        ...

    async def invalidate(self) -> None:
        """Force-discard any cached credential.

        After this call, the next ``get_token()`` must produce a freshly
        minted credential rather than a stale cached one.

        For App mode: clears the cached installation token so the next
        ``get_token()`` re-mints.
        For PAT mode: a safe no-op — static providers have nothing to
        discard and ``get_token()`` keeps returning the same token (this is
        how the service detects a credential that cannot be refreshed).
        """
        ...
