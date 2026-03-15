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
