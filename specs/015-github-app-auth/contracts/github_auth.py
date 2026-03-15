"""
Contract: GitHubAuth protocol and implementations.

This file defines the internal Python interface contracts for the auth layer.
It is a design artefact — not production code — but the signatures here
are binding for implementation.

Branch: 015-github-app-auth
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol (the shared interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class GitHubAuth(Protocol):
    """Provides a current, valid GitHub bearer token.

    Both PAT and App auth implement this protocol. All consumers
    (GitHubService, performer token injection) call `get_token()` only —
    they never interact with credential lifecycle directly.
    """

    async def get_token(self) -> str:
        """Return a current, valid GitHub bearer token string.

        - For PAT mode: returns the static token immediately.
        - For App mode: returns the cached installation token, refreshing
          it transparently if it is within 5 minutes of expiry.

        Raises:
            TransientGitHubError: token acquisition failed transiently (caller should retry).
            ValueError: misconfigured credentials (programming error, not retried).
        """
        ...


# ---------------------------------------------------------------------------
# PatAuth
# ---------------------------------------------------------------------------

class PatAuth:
    """GitHubAuth backed by a static personal access token.

    Construction:
        PatAuth(token: str)
            token  — non-empty GitHub PAT string

    Raises ValueError at construction if token is empty.
    """

    def __init__(self, token: str) -> None:
        if not token or not token.strip():
            msg = "PatAuth: token must be non-empty"
            raise ValueError(msg)
        self._token = token

    async def get_token(self) -> str:
        return self._token


# ---------------------------------------------------------------------------
# AppAuth
# ---------------------------------------------------------------------------

class AppAuth:
    """GitHubAuth backed by GitHub App installation credentials.

    Construction:
        AppAuth(app_id: int, private_key_path: Path, installation_id: int)
            app_id            — numeric GitHub App ID
            private_key_path  — path to RSA PEM private key file (must exist and be readable)
            installation_id   — numeric installation ID

    Raises ValueError at construction if the key file cannot be read.

    Thread/async safety: uses asyncio.Lock to serialise concurrent refresh attempts.
    """

    # The installation token endpoint
    TOKEN_URL_TEMPLATE = (
        "https://api.github.com/app/installations/{installation_id}/access_tokens"
    )

    # Refresh the token when fewer than this many seconds remain
    REFRESH_BUFFER_SECONDS: int = 300  # 5 minutes

    def __init__(
        self,
        app_id: int,
        private_key_path: Path,
        installation_id: int,
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_pem: bytes = self._load_key(private_key_path)
        self._cached_token: str | None = None
        self._token_expires_at: float | None = None  # monotonic seconds
        # asyncio.Lock is created lazily on first use to avoid issues with
        # instantiation outside an event loop.
        self._lock: object | None = None  # asyncio.Lock, populated on first get_token()

    @staticmethod
    def _load_key(path: Path) -> bytes:
        """Read and return PEM bytes; raise ValueError with an actionable message on failure."""
        ...  # implementation: path.read_bytes() with OSError → ValueError

    def _make_jwt(self) -> str:
        """Generate a signed RS256 JWT for the GitHub App.

        Payload:
            iss  = str(app_id)
            iat  = int(now - 60)    # backdate 60 s for clock skew
            exp  = int(now + 600)   # GitHub maximum is 10 minutes

        Returns the encoded JWT string.
        """
        ...  # implementation: jwt.encode(payload, pem, algorithm="RS256")

    async def _fetch_installation_token(self) -> tuple[str, float]:
        """POST to the installation token endpoint; return (token, expires_at_monotonic)."""
        ...  # implementation: httpx.AsyncClient().post(...) with JWT bearer

    async def get_token(self) -> str:
        """Return a cached or freshly acquired installation access token."""
        ...  # implementation: check expiry → lock → double-check → fetch if needed


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_auth(config: object) -> GitHubAuth:
    """Construct the correct GitHubAuth implementation from ProjectConfiguration.

    Args:
        config — ProjectConfiguration instance

    Returns:
        PatAuth if config.github_auth == "pat"
        AppAuth if config.github_auth == "app"

    Raises ValueError for unrecognised auth mode (defensive guard).
    """
    ...
