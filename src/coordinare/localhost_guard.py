"""Localhost guard for the dashboard (spec 144 / issue #198).

The dashboard defaults to a loopback control plane. Spec 143 adds optional
authentication; these Host/Origin checks remain active in both modes so a foreign
page in the operator's browser cannot drive the control plane.

**Two checks, two different attacks.** Neither substitutes for the other, and
dropping either one leaves a real hole:

``Origin``, on mutating methods only
    Stops cross-site request forgery. In a CSRF attack the ``Host`` really is
    ``localhost``, because the browser really is talking to localhost. Only
    ``Origin`` reveals that the *page* issuing the request is foreign.

``Host``, on every method including reads
    Stops DNS rebinding, where an attacker's domain resolves to ``127.0.0.1`` so
    the browser believes their page and the dashboard share an origin. A browser
    sends **no** ``Origin`` on a same-origin GET, so the origin check is
    structurally blind to this, and read endpoints such as
    ``GET /api/config/global`` would leak.

The complete decision table lives in
``specs/144-threat-model-trust-boundaries/contracts/localhost-guard.md``, and
``tests/unit/test_144_threat_model_trust_boundaries.py`` mirrors it row for row.

This guard authenticates nothing. A permitted local caller is fully trusted,
when optional dashboard authentication is disabled.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request, Response

_log = structlog.get_logger(__name__)

#: Methods that can change state. Classification is by method rather than by
#: path: a path list would need maintaining alongside the routes and would drift,
#: whereas every mutating verb is mutating forever.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Schemes a permitted origin may use.
_ALLOWED_SCHEMES = ("http://", "https://")

#: The literal hostname for loopback, which is not an IP and so is not caught by
#: ``ipaddress.is_loopback``.
_LOCALHOST_NAME = "localhost"


@dataclass(frozen=True, slots=True)
class RequestVerdict:
    """The result of applying the decision table to one request."""

    allowed: bool
    rejected_header: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.allowed and not (self.rejected_header and self.reason):
            msg = "a rejection must name the header and give a reason"
            raise ValueError(msg)


def _split_host_port(value: str) -> tuple[str, str | None]:
    """Split a ``Host`` header value into (host, port).

    Two details that a naive implementation gets wrong, both of which produce
    real bugs rather than cosmetic ones:

    * IPv6 in a ``Host`` header is bracketed (``[::1]:8090``), so the brackets
      must come off before the address will parse.
    * ``rsplit(":", 1)`` corrupts a **bare** IPv6 address: ``::1`` becomes
      ``:``, which no longer parses, and a legitimate IPv6 loopback caller is
      refused.
    """
    value = value.strip()

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return value, None  # malformed; caller will refuse it
        host = value[1:closing]
        remainder = value[closing + 1 :]
        port = remainder[1:] if remainder.startswith(":") else None
        return host, port

    # Only strip a port when there is exactly one colon. More than one means a
    # bare IPv6 address, which carries no port.
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, port or None

    return value, None


def _is_loopback_address(host: str) -> bool:
    """True when ``host`` is a loopback IP.

    Uses ``ipaddress`` rather than comparing against known spellings, so the
    whole ``127.0.0.0/8`` range and ``::1`` are covered without anyone
    maintaining a list. ``127.0.0.2`` is loopback and a hand-written list would
    wrongly refuse it.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard_bind(host: str) -> bool:
    """Is this a bind-all address rather than a hostname a client would send?

    ``0.0.0.0`` and ``::`` mean "listen on every interface". They are not names
    anyone legitimately puts in a ``Host`` header, so they must never become
    permitted origins.

    This matters because browsers on macOS and Linux let a page reach
    ``http://0.0.0.0:<port>``, which routes to loopback — the "0.0.0.0 day"
    quirk. Trusting the bind address would therefore hand an attacker's page a
    ``Host`` value that passes this guard and reaches a dashboard the operator
    has port-forwarded, which is precisely the request this guard exists to
    refuse.
    """
    candidate = host.strip().strip("[]").lower()
    if candidate in {"*", ""}:
        return True

    # Resolved, not string-matched. The first version of this compared against a
    # hand-written set of spellings and missed four that a server will happily
    # bind to: "::0", "0:0:0:0:0:0:0:0", the fully-expanded IPv6 zero, and the
    # bare "0" (which the resolver expands to 0.0.0.0, as do "00.00.00.00" and
    # "0x0"). Any of those as ``dashboard_host`` would have put the hole straight
    # back. Enumerating spellings of a number is a losing game; asking what the
    # value resolves to is not.
    #
    # AI_NUMERICHOST keeps this to pure parsing: no DNS lookup can be triggered
    # by a configuration value, and a real hostname is simply rejected here and
    # left to the trusted-host list where it belongs.
    try:
        resolved = socket.getaddrinfo(
            candidate, None, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
        )
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    return any(ipaddress.ip_address(info[4][0]).is_unspecified for info in resolved)


def is_loopback_bind(host: str) -> bool:
    """True when binding to ``host`` keeps a server off the network.

    Shared by the guard and by the startup warning so there is exactly one
    definition of "local". A second implementation would be free to drift, and
    the two disagreeing is how a warning stops firing for a bind that is in fact
    exposed.

    Note ``0.0.0.0`` and ``::`` are **not** loopback: binding to every interface
    is the opposite of binding to the local one, and conflating them is the
    mistake this function exists to prevent.
    """
    value = (host or "").strip().lower()
    if value == _LOCALHOST_NAME:
        return True
    return _is_loopback_address(value)


def warn_if_dashboard_exposed(host: str, port: int, *, log: object | None = None) -> bool:
    """Warn when the dashboard is bound beyond loopback. Returns whether it warned.

    Lives here rather than inline in ``__main__`` for two reasons. It keeps one
    definition of "local" next to the guard that enforces it, and it makes the
    warning path *testable*: as an inline block it was never executed by any
    test, and an undefined logger name in it went unnoticed until lint caught it.
    A warning that raises instead of warning is worse than no warning, because it
    fails exactly when it was needed.

    Only fires for a non-loopback bind (FR-021): a warning emitted on every start
    is a warning nobody reads.
    """
    if is_loopback_bind(host):
        return False

    (log or _log).warning(
        "dashboard_exposed_without_authentication",
        dashboard_host=host,
        dashboard_port=port,
        detail=(
            "The dashboard is bound beyond loopback and has NO authentication. "
            "Anyone who can reach this address can change coordinare's configuration, "
            "cancel work, and delete symphonies. Bind it to 127.0.0.1 unless the "
            "network it is on is fully trusted. See docs/security/threat-model.md."
        ),
        # Without this second half an operator hits a wall of 403s with no clue
        # why: the localhost guard refuses the very hostname they browse to, and
        # nothing else tells them the setting exists. The guard is behaving
        # correctly (they must declare which hostnames they will use), but a
        # correct refusal nobody can diagnose is still an outage.
        action_required=(
            "The dashboard will reject requests for any hostname you have not "
            "declared. Add the address you browse to (for example "
            "'coordinare.example:8090' or the machine's LAN address) to "
            "trusted_dashboard_hosts in your configuration, or every request will "
            "be refused with 403."
        ),
    )
    return True


@dataclass(frozen=True, slots=True)
class PermittedOrigins:
    """What this dashboard accepts, computed once at application construction.

    Immutable on purpose: a guard whose permitted set can change at runtime is a
    guard with a race in it.
    """

    hosts: frozenset[str]
    #: Retained for the rejection message only. Deliberately NOT used to accept
    #: or reject a request; see is_host_allowed for why comparing it was both
    #: useless and harmful.
    port: int

    # ---- individual checks -------------------------------------------------

    def is_host_allowed(self, value: str | None) -> bool:
        """FR-011, FR-013, FR-014."""
        if not value:
            return False

        # Reject any internal whitespace outright rather than letting a later
        # strip launder it. An earlier version stripped in two places, so
        # "http:// 127.0.0.1:8090" parsed as a permitted origin: the outer call
        # sliced off the scheme leaving a leading space, and _split_host_port
        # stripped it away again. Neither a Host nor an Origin header may
        # legitimately contain whitespace, so refusing is both correct and
        # simpler than normalising.
        if any(ch.isspace() for ch in value.strip()):
            return False

        host, _port = _split_host_port(value)

        # The port is deliberately NOT compared. It was, and that was wrong twice
        # over.
        #
        # It bought no security. A Host port is set by the browser from the URL
        # it connected to, and a request cannot arrive on a port the server is
        # not listening on, so a mismatched port can only come from a non-browser
        # client, which this guard never authenticated anyway. DNS rebinding is
        # caught by the HOSTNAME check, which is unaffected.
        #
        # And it broke real deployments. Any app served on a port other than the
        # configured dashboard_port refused every request. The end-to-end browser
        # suite binds a random free port, so the browser sent
        # "Host: 127.0.0.1:54321", the guard answered 403 to every page load, and
        # the job hung until it timed out.
        host = host.lower()
        return host in self.hosts or _is_loopback_address(host)

    def is_origin_allowed(self, value: str | None) -> bool:
        """FR-009. ``null``, opaque, and malformed origins are refused."""
        if not value:
            return False

        origin = value.strip().lower()
        if any(ch.isspace() for ch in origin):
            return False
        for scheme in _ALLOWED_SCHEMES:
            if origin.startswith(scheme):
                return self.is_host_allowed(origin[len(scheme) :])
        return False

    # ---- the decision table ------------------------------------------------

    def check(self, method: str, host: str | None, origin: str | None) -> RequestVerdict:
        """Apply the decision table. First match wins.

        See ``contracts/localhost-guard.md``. Row 4 (an absent ``Origin`` is
        allowed) is the one that looks wrong and is not: browsers always send
        ``Origin`` on non-GET requests, so its absence means the caller is not a
        browser, and cross-site request forgery requires a browser. Rejecting it
        would break curl, scripts, and monitoring probes for no security gain,
        and row 2 still guards those callers.
        """
        # Row 1
        if not host:
            return RequestVerdict(
                allowed=False,
                rejected_header="Host",
                reason="request has no Host header, which HTTP/1.1 requires",
            )

        # Row 2 — applies to reads as well, which is the DNS-rebinding defence.
        if not self.is_host_allowed(host):
            return RequestVerdict(
                allowed=False,
                rejected_header="Host",
                reason=(
                    "Host is not a permitted local address. The dashboard only serves "
                    f"loopback on port {self.port}. If you are fronting it with a proxy, "
                    "add that hostname to trusted_dashboard_hosts in your configuration."
                ),
            )

        # Row 3
        if method.upper() not in MUTATING_METHODS:
            return RequestVerdict(allowed=True)

        # Row 4
        if not origin:
            return RequestVerdict(allowed=True)

        # Row 5
        if not self.is_origin_allowed(origin):
            return RequestVerdict(
                allowed=False,
                rejected_header="Origin",
                reason=(
                    "Origin is not a permitted local origin. A page from another site "
                    "cannot make changes through the dashboard."
                ),
            )

        # Row 6
        return RequestVerdict(allowed=True)


def build_permitted(
    dashboard_host: str,
    dashboard_port: int,
    trusted_hosts: list[str] | None = None,
) -> PermittedOrigins:
    """Derive the permitted set from configuration (FR-013, FR-015).

    ``trusted_hosts`` is the operator's explicit opt-in for a proxy hostname. It
    is empty by default, so the safe posture is what you get by doing nothing.
    ``X-Forwarded-Host`` is deliberately not consulted anywhere: it is
    attacker-controlled unless a proxy overwrites it, and honouring it by default
    would silently undo this guard.

    **A bind-all ``dashboard_host`` is not added.** Binding to ``0.0.0.0`` says
    where to listen; it is not a name a client would ever legitimately send.
    Admitting it re-opened the exact hole this guard closes, because browsers on
    macOS and Linux route ``http://0.0.0.0:<port>`` to loopback, so a malicious
    page could reach a port-forwarded dashboard with a ``Host`` the guard
    accepted. Anyone who genuinely needs a non-loopback name must still name it
    in ``trusted_hosts``, which is the deliberate, visible opt-in.
    """
    hosts = {_LOCALHOST_NAME}
    if not _is_wildcard_bind(dashboard_host):
        hosts.add(dashboard_host.strip().lower())
    hosts.update(host.strip().lower() for host in (trusted_hosts or []) if host.strip())
    return PermittedOrigins(hosts=frozenset(hosts), port=dashboard_port)


def install_localhost_guard(
    app: FastAPI,
    permitted: PermittedOrigins,
    exempt_paths: frozenset[str] | None = None,
) -> None:
    """Install the guard as middleware (FR-012).

    Middleware, not per-route dependencies, so a route added tomorrow is guarded
    without its author doing anything. With 23 mutating routes already present,
    an opt-in mechanism is one forgotten annotation away from a hole, and the
    failure is silent: the route works, it is simply unguarded.

    ``exempt_paths`` is the deliberate escape hatch, and it exists for exactly
    one reason today: the GitHub webhook endpoint. That route is *designed* to be
    called from the internet, and it carries its own authentication (an HMAC
    signature over the body, verified against a shared secret, rejecting with
    401). Guarding it would break webhooks entirely, since GitHub is by
    definition not a local caller.

    Anything added here MUST authenticate itself. An exemption without its own
    authentication is simply an unguarded route, which is the state this module
    exists to prevent. Exemptions are exact path matches, not prefixes, so a
    single entry cannot silently widen to cover a subtree.
    """
    from fastapi.responses import JSONResponse

    exempt = exempt_paths or frozenset()

    @app.middleware("http")
    async def _localhost_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in exempt:
            return await call_next(request)

        verdict = permitted.check(
            request.method,
            host=request.headers.get("host"),
            origin=request.headers.get("origin"),
        )
        if verdict.allowed:
            return await call_next(request)

        # Deliberately does not echo the offending header value back. The
        # message says what was expected, which is what an operator debugging a
        # misconfiguration needs, without reflecting attacker-controlled content.
        _log.warning(
            "dashboard_request_rejected",
            method=request.method,
            path=str(request.url.path),
            rejected_header=verdict.rejected_header,
        )
        return JSONResponse(
            status_code=403,
            content={
                "error": "rejected_by_localhost_guard",
                "rejected_header": verdict.rejected_header,
                "detail": verdict.reason,
            },
        )
