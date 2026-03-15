"""AppAuth — GitHub App installation token with JWT generation and auto-refresh."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import jwt

if TYPE_CHECKING:
    from pathlib import Path


class AppAuth:
    """GitHubAuth backed by GitHub App installation credentials.

    Reads the private key once at construction. Generates RS256 JWTs
    and exchanges them for installation access tokens via the GitHub REST API.
    Tokens are cached and refreshed automatically when fewer than 5 minutes remain.
    """

    TOKEN_URL_TEMPLATE = (
        "https://api.github.com/app/installations/{installation_id}/access_tokens"
    )
    REFRESH_BUFFER_SECONDS: int = 300  # refresh when < 5 min remain

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
        self._lock = asyncio.Lock()  # initialised eagerly; safe once event loop exists

    @staticmethod
    def _load_key(path: Path) -> bytes:
        """Read PEM bytes; raise ValueError with actionable message on failure."""
        try:
            return path.read_bytes()
        except OSError as exc:
            msg = (
                f"github_private_key_path {str(path)!r} does not exist or is not readable: {exc}"
            )
            raise ValueError(msg) from exc

    def _make_jwt(self) -> str:
        """Generate a signed RS256 JWT for the GitHub App."""
        now = int(time.time())
        payload = {
            "iss": str(self._app_id),
            "iat": now - 60,   # backdate 60 s for clock skew
            "exp": now + 600,  # GitHub max: 10 minutes
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    async def _fetch_installation_token(self) -> tuple[str, float]:
        """POST to the installation token endpoint; return (token, expires_at_monotonic).

        Raises:
            PermanentGitHubError: on 4xx responses (invalid credentials, not retryable).
            TransientGitHubError: on 5xx / network errors (retryable).
        """
        from coordinare.services.github import PermanentGitHubError, TransientGitHubError

        token_jwt = self._make_jwt()
        url = self.TOKEN_URL_TEMPLATE.format(installation_id=self._installation_id)
        headers = {
            "Authorization": f"Bearer {token_jwt}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers)

        if response.status_code in (200, 201):
            pass  # success — parse below
        elif 400 <= response.status_code < 500:
            msg = f"installation token request rejected (HTTP {response.status_code}): check app_id, installation_id, and private key"
            raise PermanentGitHubError(msg)
        else:
            msg = f"installation token request failed (HTTP {response.status_code})"
            raise TransientGitHubError(msg)

        try:
            data = response.json()
            token: str = data["token"]
            expires_dt = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            msg = f"installation token response malformed: {exc}"
            raise TransientGitHubError(msg) from exc

        remaining = (expires_dt - datetime.now(UTC)).total_seconds()
        expires_monotonic = time.monotonic() + remaining
        return token, expires_monotonic

    async def get_token(self) -> str:
        """Return a cached or freshly acquired installation access token."""
        # Fast path: cached and not near expiry
        if (
            self._cached_token is not None
            and self._token_expires_at is not None
            and time.monotonic() < self._token_expires_at - self.REFRESH_BUFFER_SECONDS
        ):
            return self._cached_token

        async with self._lock:
            # Double-check after acquiring lock
            if (
                self._cached_token is not None
                and self._token_expires_at is not None
                and time.monotonic() < self._token_expires_at - self.REFRESH_BUFFER_SECONDS
            ):
                return self._cached_token

            token, expires_at = await self._fetch_installation_token()
            self._cached_token = token
            self._token_expires_at = expires_at
            return token
