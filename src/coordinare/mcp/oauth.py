"""RFC 7662 introspection token verifier for the OAuth 2.1 resource server (553).

Fails closed: any transport, status, or payload failure denies the token with
one structured ``mcp_oauth.introspection_failed`` line. Token text never
reaches logs — only error types and payload-derived identities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog
from mcp.server.auth.provider import AccessToken

if TYPE_CHECKING:
    from coordinare.mcp.config import McpOAuthConfig

_INTROSPECTION_FAILED = "mcp_oauth.introspection_failed"
_HTTP_ERROR_STATUS = 400


class IntrospectionTokenVerifier:
    """SDK ``TokenVerifier`` implementation backed by RFC 7662 introspection."""

    def __init__(
        self,
        config: McpOAuthConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._log = structlog.get_logger(__name__)

    async def verify_token(self, token: str) -> AccessToken | None:
        payload = await self._introspect(token)
        if payload is None:
            return None
        if not payload.get("active", False):
            self._log.debug("mcp_oauth.token_inactive")
            return None
        if not self._audience_ok(payload):
            self._log.info("mcp_oauth.token_rejected", reason="audience_mismatch")
            return None
        return self._to_access_token(token, payload)

    async def _introspect(self, token: str) -> dict[str, Any] | None:
        post_kwargs: dict[str, Any] = {"data": {"token": token}}
        if self._config.client_id and self._config.client_secret:
            post_kwargs["auth"] = httpx.BasicAuth(
                self._config.client_id,
                self._config.client_secret.get_secret_value(),
            )
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._config.timeout_seconds,
            ) as client:
                response = await client.post(
                    str(self._config.introspection_url),
                    **post_kwargs,
                )
        except httpx.HTTPError as exc:
            self._log.error(
                _INTROSPECTION_FAILED,
                kind="transport",
                error_type=type(exc).__name__,
            )
            return None
        if response.status_code >= _HTTP_ERROR_STATUS:
            self._log.error(
                _INTROSPECTION_FAILED,
                kind="status",
                status_code=response.status_code,
            )
            return None
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            self._log.error(_INTROSPECTION_FAILED, kind="malformed_payload")
            return None
        return payload

    def _audience_ok(self, payload: dict[str, Any]) -> bool:
        expected = self._config.resource_server_url
        if expected is None:
            return True
        aud = payload.get("aud")
        audiences = [aud] if isinstance(aud, str) else (aud or [])
        return str(expected) in audiences

    def _to_access_token(self, token: str, payload: dict[str, Any]) -> AccessToken:
        raw_scopes = payload.get("scope", "")
        scopes = (
            [part for part in raw_scopes.split(" ") if part]
            if isinstance(raw_scopes, str)
            else list(raw_scopes)
        )
        expires_at = payload.get("exp")
        return AccessToken(
            token=token,
            client_id=payload.get("client_id", "unknown"),
            scopes=scopes,
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=payload.get("aud"),
            subject=payload.get("sub"),
            claims=payload,
        )


__all__ = ["IntrospectionTokenVerifier"]
