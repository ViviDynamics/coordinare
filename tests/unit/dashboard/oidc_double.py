"""A deterministic OIDC provider double for dashboard OIDC tests (497).

Serves discovery, token and JWKS endpoints as an ASGI app. The dashboard's
provider client is pointed at it with ``httpx.ASGITransport``, so no test
touches a live network. Issued authorization codes are single-use; the ID
token is signed with an RSA key minted for the test session.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport
from jwt import PyJWK

ISSUER = "https://provider.test"
CLIENT_ID = "coordinare-dashboard"
CLIENT_SECRET = "double-client-secret-0123456789012345678"
SUBJECT = "operator@example.test"
KID = "test-key-1"


class ProviderDouble:
    """Minimal conformant OIDC provider: discovery, token exchange, JWKS."""

    def __init__(self, *, id_nonce: str = "nonce-from-authorization") -> None:
        self.issuer = ISSUER
        self.client_id = CLIENT_ID
        self.client_secret = CLIENT_SECRET
        self.subject = SUBJECT
        self.id_nonce = id_nonce
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._issued_codes: set[str] = set()
        self._next_code = 0
        self.token_requests: list[dict[str, str]] = []

    # --- ID token ---------------------------------------------------------

    def id_token(self, *, nonce: str | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": self.subject,
            "exp": now + 300,
            "iat": now,
            "nonce": nonce if nonce is not None else self.id_nonce,
        }
        claims.update(overrides)
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": KID})

    # --- authorization-code bookkeeping -----------------------------------

    def issue_code(self) -> str:
        self._next_code += 1
        code = f"auth-code-{self._next_code:04d}"
        self._issued_codes.add(code)
        return code

    def consume_code(self, code: str) -> bool:
        if code not in self._issued_codes:
            return False
        self._issued_codes.discard(code)
        return True

    # --- ASGI app ----------------------------------------------------------

    def asgi_app(self):
        provider = self

        async def app(scope, receive, send):
            if scope["type"] != "http":
                await provider._plain(send, 400, b"no ws")
                return
            from starlette.requests import Request

            request = Request(scope, receive=receive)
            path = request.url.path
            if path == "/.well-known/openid-configuration":
                await provider._plain(
                    send, 200, json.dumps(provider.discovery()).encode(),
                    content_type="application/json",
                )
            elif path == "/token":
                await provider._token(request, send)
            elif path == "/jwks.json":
                await provider._plain(
                    send, 200, json.dumps(provider.public_jwks()).encode(),
                    content_type="application/json",
                )
            else:
                await provider._plain(send, 404, b"not found")

        return app

    def transport(self) -> ASGITransport:
        return ASGITransport(app=self.asgi_app())

    def discovery(self) -> dict[str, str]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks.json",
        }

    def public_jwks(self) -> dict[str, list[dict[str, Any]]]:
        numbers = self._key.public_key().public_numbers()
        modulus = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return {"keys": [{
            "kty": "RSA", "kid": KID, "alg": "RS256", "use": "sig",
            "n": b64(modulus), "e": b64(numbers.e.to_bytes(3, "big")),
        }]}

    async def _token(self, request, send) -> None:
        from urllib.parse import parse_qs

        body = await request.body()
        fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        self.token_requests.append(fields)
        if fields.get("grant_type") != "authorization_code":
            await self._plain(send, 400, b"bad grant_type")
            return
        if fields.get("client_id") != self.client_id or fields.get("client_secret") != self.client_secret:
            await self._plain(send, 401, b"bad client credentials")
            return
        if not self.consume_code(fields.get("code", "")):
            await self._plain(send, 400, b"unknown or consumed code")
            return
        body = json.dumps({
            "access_token": "access-token",
            "token_type": "Bearer",
            "id_token": self.id_token(),
        }).encode()
        await self._plain(send, 200, body, content_type="application/json")

    async def _plain(self, send, status: int, body: bytes, content_type: str = "text/plain") -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode())],
        })
        await send({"type": "http.response.body", "body": body})


def verification_key_for(double: ProviderDouble, token: str):
    header_kid = jwt.get_unverified_header(token)["kid"]
    for jwk in double.public_jwks()["keys"]:
        if jwk["kid"] == header_kid:
            return PyJWK.from_dict(jwk).key
    raise KeyError(header_kid)
