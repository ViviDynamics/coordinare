"""OIDC Authorization Code login for the dashboard (497).

Lives beside :mod:`coordinare.dashboard_auth` at package top level: it is needed
at daemon boot (validation) and by the dashboard middleware, but it must not
import the FastAPI-heavy ``coordinare.dashboard`` package.

The provider I/O stays inside this module. Routes in
``coordinare.dashboard.routers.oidc`` translate :class:`OidcLoginError` into
HTTP responses; every error message is a fixed diagnostic that never contains
configuration secrets or provider response bodies.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import jwt
import structlog
from jwt import PyJWK
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from coordinare.localhost_guard import _is_wildcard_bind

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import DashboardOidcConfig

_log = structlog.get_logger(__name__)

STATE_MAX_AGE_SECONDS = 300
PROVIDER_TIMEOUT_SECONDS = 10.0
PENDING_CAPACITY = 1024
_HTTP_OK = 200
SESSION_CAPACITY = 4096
SESSION_FILE_NAME = "dashboard_oidc_sessions.json"
_SESSION_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_URL_CONTROL_CHARACTERS = frozenset(map(chr, range(32))) | frozenset({"\x7f"})

SESSION_COOKIE = "coordinare_session"
STATE_COOKIE = "oidc_state"
NONCE_COOKIE = "oidc_nonce"


class OidcLoginError(Exception):
    """A fixed, secret-free diagnostic for a failed login step."""

    def __init__(self, reason: str, *, provider_unreachable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.provider_unreachable = provider_unreachable


def validate_dashboard_oidc(
    config: DashboardOidcConfig, *, dashboard_host: str,
    trusted_hosts: list[str] | None,
) -> None:
    """Refuse a redirect target outside the operator's host set at boot time.

    The redirect host is the only place an authorization code can be delivered,
    so an attacker-chosen value would hand the code to a third party. The set
    mirrors the localhost guard's: bind-all hosts are excluded and every entry
    is compared lowercased, so a value the guard would refuse as a ``Host``
    cannot be trusted here either.
    """
    host = (urlparse(config.redirect_url).hostname or "").lower()
    permitted: set[str] = set()
    if not _is_wildcard_bind(dashboard_host):
        permitted.add(dashboard_host.strip().lower())
    permitted.update(
        h.strip().lower() for h in (trusted_hosts or []) if h.strip()
    )
    if host not in permitted:
        msg = (
            "dashboard_oidc.redirect_url host is not trusted: it must use "
            f"dashboard_host {dashboard_host!r} or a trusted_dashboard_hosts entry"
        )
        raise ValueError(msg)


@dataclass(frozen=True)
class OidcIdentity:
    """Verified profile claims and a server-only hint for provider logout."""

    subject: str
    name: str = ""
    email: str = ""
    id_token: str = field(default="", repr=False)
    # 157: the short login name (preferred_username) and the identity provider's host, for the header chip.
    username: str = ""
    provider: str = ""


class _SessionTable(BaseModel):
    """Versioned private snapshot with hashed cookie keys and finite expiries."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1] = 1
    sessions: dict[
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        tuple[OidcIdentity, FiniteFloat],
    ]


class OidcSessionStore:
    """Bounded browser sessions, persisted when a private state path is supplied.

    Cookie values are hashed before entering the table. Writes happen only on
    changes, using a private temporary file and atomic replacement. One daemon
    owns the file; this is not a shared store for concurrent replicas.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.time,
        capacity: int = SESSION_CAPACITY,
        path: Path | None = None,
    ) -> None:
        if capacity < 1:
            msg = "OIDC session capacity must be positive"
            raise ValueError(msg)
        self._sessions: dict[str, tuple[OidcIdentity, float]] = {}
        self._clock = clock
        self._capacity = capacity
        self._path = path
        self._load()

    def _prune(self) -> bool:
        previous_size = len(self._sessions)
        now = self._clock()
        self._sessions = {
            key: entry for key, entry in self._sessions.items() if entry[1] > now
        }
        while len(self._sessions) > self._capacity:
            self._sessions.pop(next(iter(self._sessions)))
        return len(self._sessions) != previous_size

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            table = _SessionTable.model_validate_json(self._path.read_bytes())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            _log.warning("dashboard_oidc.sessions_load_failed", error=type(exc).__name__)
            return
        self._sessions = table.sessions
        if self._prune():
            # A read-only volume must not stop startup, even during cleanup.
            with contextlib.suppress(OSError):
                self._persist()

    def _persist(self) -> None:
        if self._path is None:
            return
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent,
                prefix=f".{self._path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), _SESSION_FILE_MODE)
                stream.write(_SessionTable(sessions=self._sessions).model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            _log.warning("dashboard_oidc.sessions_write_failed", error=type(exc).__name__)
            raise
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def create(self, subject: str | OidcIdentity, *, lifetime_seconds: int) -> str:
        self._prune()
        while len(self._sessions) >= self._capacity:
            self._sessions.pop(next(iter(self._sessions)))
        value = secrets.token_urlsafe(32)
        identity = OidcIdentity(subject) if isinstance(subject, str) else subject
        self._sessions[hashlib.sha256(value.encode()).hexdigest()] = (
            identity, self._clock() + lifetime_seconds,
        )
        self._persist()
        return value

    def lookup(self, value: str) -> str | None:
        identity = self.identity(value)
        return identity.subject if identity is not None else None

    def identity(self, value: str) -> OidcIdentity | None:
        key = hashlib.sha256(value.encode()).hexdigest()
        entry = self._sessions.get(key)
        if entry is None:
            return None
        identity, expires_at = entry
        if self._clock() >= expires_at:
            self._sessions.pop(key, None)
            self._persist()
            return None
        return identity

    def revoke(self, value: str) -> None:
        key = hashlib.sha256(value.encode()).hexdigest()
        previous = self._sessions.copy()
        if self._sessions.pop(key, None) is not None:
            try:
                self._persist()
            except OSError:
                # Keep a failed logout retryable until the durable removal succeeds.
                self._sessions = previous
                raise


@dataclass
class _PendingLogin:
    nonce: str
    created_at: float


class OidcFlow:
    """One configured provider: discovery, login redirect, exchange, sessions."""

    def __init__(
        self, config: DashboardOidcConfig, *,
        client: httpx.AsyncClient | None = None,
        session_path: Path | None = None,
    ) -> None:
        self.config = config
        self.sessions = OidcSessionStore(path=session_path)
        self._client = client
        self._discovery: dict[str, str] | None = None
        self._discovery_lock = asyncio.Lock()
        self._pending: dict[str, _PendingLogin] = {}

    # --- provider documents -------------------------------------------------

    async def _discovery_document(self) -> dict[str, str]:
        if self._discovery is not None:
            return self._discovery
        async with self._discovery_lock:
            if self._discovery is None:
                self._discovery = await self._load_discovery()
        return self._discovery

    async def _load_discovery(self) -> dict[str, str]:
        try:
            response = await self._http().get(
                self.config.discovery_url, timeout=PROVIDER_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            msg = "OIDC provider discovery unreachable"
            raise OidcLoginError(msg, provider_unreachable=True) from exc
        if response.status_code != _HTTP_OK:
            msg = f"OIDC provider discovery returned HTTP {response.status_code}"
            raise OidcLoginError(msg, provider_unreachable=True)
        document = _json_body(response, "discovery")
        required = ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer")
        endpoints: dict[str, str] = {}
        for key in required:
            value = document.get(key)
            parsed = urlparse(value) if isinstance(value, str) else None
            fetched = key != "issuer"
            if (
                not isinstance(value, str)
                or not value.strip()
                or (
                    fetched
                    and (parsed is None or parsed.scheme != "https" or not parsed.hostname)
                )
            ):
                msg = "OIDC provider discovery document has an invalid " + key
                raise OidcLoginError(msg, provider_unreachable=True)
            endpoints[key] = value.strip()
        logout = document.get("end_session_endpoint")
        if isinstance(logout, str) and _safe_logout_endpoint(logout):
            endpoints["end_session_endpoint"] = logout
        return endpoints


    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_SECONDS)
        return self._client

    # --- login round trip ------------------------------------------------------

    async def authorization_redirect(self) -> tuple[str, str, str]:
        """Start a login: returns (provider_url, state, nonce)."""
        discovery = await self._discovery_document()
        while len(self._pending) >= PENDING_CAPACITY:
            oldest = min(self._pending, key=lambda s: self._pending[s].created_at)
            self._pending.pop(oldest)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        self._pending[state] = _PendingLogin(nonce=nonce, created_at=time.time())
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_url,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        return f"{discovery['authorization_endpoint']}?{urlencode(params)}", state, nonce

    def consume_state(self, state: str | None) -> str | None:
        """Single-use: hand back the paired nonce, or None when unknown/stale."""
        if not state:
            return None
        entry = self._pending.pop(state, None)
        if entry is None or time.time() - entry.created_at > STATE_MAX_AGE_SECONDS:
            return None
        return entry.nonce

    async def exchange(self, code: str, expected_nonce: str) -> OidcIdentity:
        """Exchange the code and return only verified ID-token profile claims."""
        discovery = await self._discovery_document()
        try:
            response = await self._http().post(
                discovery["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.redirect_url,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret.get_secret_value(),
                },
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            msg = "OIDC provider token endpoint unreachable"
            raise OidcLoginError(msg, provider_unreachable=True) from exc
        if response.status_code != _HTTP_OK:
            _log.info(
                "dashboard_oidc.exchange_refused", provider_status=response.status_code,
            )
            msg = "OIDC token exchange refused by provider"
            raise OidcLoginError(msg)
        payload = _json_body(response, "token response")
        token = payload.get("id_token")
        if not isinstance(token, str) or not token:
            msg = "OIDC token response missing id_token"
            raise OidcLoginError(msg, provider_unreachable=True)
        return await self._verify_id_token(token, expected_nonce, discovery)

    async def _verify_id_token(
        self, token: str, expected_nonce: str, discovery: dict[str, str],
    ) -> OidcIdentity:
        key = await self._signing_key(token, discovery)
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"],
                audience=self.config.client_id, issuer=discovery["issuer"],
            )
        except jwt.PyJWTError as exc:
            _log.info("dashboard_oidc.id_token_refused", error=type(exc).__name__)
            msg = "OIDC ID token verification failed"
            raise OidcLoginError(msg) from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), expected_nonce):
            msg = "OIDC ID token nonce mismatch"
            raise OidcLoginError(msg)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            msg = "OIDC ID token missing sub claim"
            raise OidcLoginError(msg)
        name = claims.get("name") or claims.get("preferred_username")
        email = claims.get("email")
        username = claims.get("preferred_username")
        return OidcIdentity(
            subject=subject, name=name if isinstance(name, str) else "",
            email=email if isinstance(email, str) else "", id_token=token,
            username=username if isinstance(username, str) else "",
            provider=urlparse(str(discovery["issuer"])).netloc,
        )

    async def _signing_key(self, token: str, discovery: dict[str, str]) -> Any:
        try:
            header_kid = jwt.get_unverified_header(token).get("kid")
            response = await self._http().get(
                discovery["jwks_uri"], timeout=PROVIDER_TIMEOUT_SECONDS,
            )
        except (httpx.HTTPError, jwt.PyJWTError) as exc:
            msg = "OIDC provider JWKS unreachable"
            raise OidcLoginError(msg, provider_unreachable=True) from exc
        if response.status_code != _HTTP_OK:
            msg = "OIDC provider JWKS unreachable"
            raise OidcLoginError(msg, provider_unreachable=True)
        jwks = _json_body(response, "jwks")
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not all(
            isinstance(key, dict) for key in keys
        ):
            msg = "OIDC provider JWKS document is malformed"
            raise OidcLoginError(msg, provider_unreachable=True)
        for jwk in keys:
            if jwk.get("kid") == header_kid:
                try:
                    return PyJWK.from_dict(jwk).key
                except jwt.PyJWKError as exc:
                    msg = "OIDC provider JWKS key unusable"
                    raise OidcLoginError(
                        msg, provider_unreachable=True,
                    ) from exc
        msg = "no JWKS key matches the ID token key id"
        raise OidcLoginError(msg)

    def start_session(self, subject: str | OidcIdentity) -> tuple[str, int]:
        """Create a browser session; returns (cookie_value, lifetime_seconds)."""
        lifetime = self.config.session_hours * 3600
        return self.sessions.create(subject, lifetime_seconds=lifetime), lifetime

    def logout_redirect(self, identity: OidcIdentity | None) -> str | None:
        """Use validated discovery from login; never block local logout on I/O."""
        endpoint = (self._discovery or {}).get("end_session_endpoint")
        if identity is None or not identity.id_token or not endpoint:
            return None
        return_url = urlparse(self.config.redirect_url)._replace(
            path="/oidc/signed-out", params="", query="", fragment="",
        ).geturl()
        parsed = urlparse(endpoint)
        params = dict(parse_qsl(parsed.query))
        params.update({
            "id_token_hint": identity.id_token, "client_id": self.config.client_id,
            "post_logout_redirect_uri": return_url,
        })
        return parsed._replace(query=urlencode(params)).geturl()


def _safe_logout_endpoint(value: str) -> bool:
    if any(char.isspace() or char in _URL_CONTROL_CHARACTERS for char in value):
        return False
    try:
        parsed = urlparse(value)
        _ = parsed.port
        return bool(parsed.scheme == "https" and parsed.hostname and not (
            parsed.username or parsed.password or parsed.fragment
        ))
    except ValueError:
        return False


def _json_body(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError as exc:
        msg = f"OIDC provider {label} is not valid JSON"
        raise OidcLoginError(msg, provider_unreachable=True) from exc
    if not isinstance(document, dict):
        msg = f"OIDC provider {label} is not a JSON object"
        raise OidcLoginError(msg, provider_unreachable=True)
    return document
