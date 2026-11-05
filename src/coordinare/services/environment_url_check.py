"""The daemon requests every literal URL an install command downloads before the manifest is executed.

Dettmore e2e-56 spent an install generation (22 minutes) on install-ruby fetching a guessed Debian mirror path that
answered 404. A URL the daemon finds missing (404 or 410) is given to the independent reviewer as a daemon fact, and
the reviewer cannot accept while a finding does not name the command, so its author corrects it before execution.

Only a definite answer counts: a timeout, a refused or unsafe address, or any other status leaves the URL unjudged.
The daemon never requests a private, loopback or link-local address, also not through a redirect, and never sends
credentials or reads a body.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
from coordinare_service_inference.environment.static_rules import fetched_urls

from coordinare.services.environment_findings import FindingValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare_service_inference.environment.findings import FindingRecord
    from coordinare_service_inference.environment.models import (
        CoverageAssessmentProposal,
        EnvironmentManifest,
    )

log = structlog.get_logger(__name__)

MISSING_STATUSES = frozenset({404, 410})
# A server that refuses HEAD, or answers it as missing, may still serve the file to GET.
_HEAD_REFUSED = frozenset({400, 403, 404, 405, 410, 501})
# NAT64 (RFC 6052) addresses carry an IPv4 address in their last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_URLS = 32
MAX_REDIRECTS = 5
REQUEST_SECONDS = 10.0
CHECK_SECONDS = 45.0

URL_CHECK_NOTE = (
    "daemon_url_checks lists install URLs the daemon requested itself before execution: each answered HTTP 404 or "
    "410 (a HEAD and then a GET), so the command naming it most likely fails as written. Report an execution_definition "
    "finding whose affected_ids name each listed command (or keep open an assigned finding that names it); an accept "
    "verdict is refused until then, except for an entry marked reported_before, which an earlier manifest of this "
    "attempt was already corrected for and which you judge on the evidence. URLs built from variables are not checked.")


def install_urls(manifest: EnvironmentManifest) -> dict[str, list[str]]:
    """Each literal http(s) URL in an install command's argv, with the ids of the commands naming it, first seen first."""
    found: dict[str, list[str]] = {}
    for command in manifest.install:
        for url in fetched_urls(command):
            if not urlsplit(url).hostname:
                continue
            if url not in found and len(found) >= MAX_URLS:
                log.warning("environment_url_check_truncated", checked=MAX_URLS)
                return found
            owners = found.setdefault(url, [])
            if command.id not in owners:
                owners.append(command.id)
    return found


async def _public(host: str, port: int) -> bool:
    """Every address the host resolves to is globally routable."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port)
    except OSError:
        return False
    addresses = [ipaddress.ip_address(info[4][0].split('%', 1)[0]) for info in infos]
    return bool(addresses) and all(_global(address) for address in addresses)


def _global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable, also for the IPv4 address an IPv6 transition form carries (mapped, 6to4, Teredo, NAT64)."""
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour, address.teredo[1] if address.teredo else None,
                    ipaddress.IPv4Address(int(address) & 0xFFFFFFFF) if address in _NAT64 else None]
        return all(inner is None or inner.is_global for inner in embedded)
    return True


async def url_status(client: Any, url: str) -> int | None:
    """The final HTTP status of ``url`` after safe redirects, or None when it cannot be judged."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        if not await _public(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)):
            return None
        response = await client.head(url)
        if response.status_code in _HEAD_REFUSED:
            async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as streamed:
                response = streamed
        if response.status_code in _REDIRECTS and (location := response.headers.get("location")):
            url = urljoin(url, location)
            continue
        return int(response.status_code)
    return None


async def missing_install_urls(manifest: EnvironmentManifest, *, client: Any = None) -> list[dict[str, Any]]:
    """The install URLs that answered 404 or 410, each with the ids of the commands that download it."""
    urls = install_urls(manifest)
    if not urls:
        return []
    owned = client is None
    # No proxy from the environment: the address checked must be the address connected to.
    client = client or httpx.AsyncClient(timeout=REQUEST_SECONDS, follow_redirects=False, trust_env=False,
                                         headers={"User-Agent": "coordinare-environment-url-check"})

    async def judged(url: str) -> int | None:
        try:
            return await url_status(client, url)
        except (httpx.HTTPError, OSError, ValueError):
            return None

    try:
        async with asyncio.timeout(CHECK_SECONDS):
            statuses = await asyncio.gather(*(judged(url) for url in urls))
    except TimeoutError:
        log.warning("environment_url_check_timeout", urls=len(urls))
        return []
    finally:
        if owned:
            await client.aclose()
    missing = [{"url": url, "status": status, "command_ids": urls[url]}
               for url, status in zip(urls, statuses, strict=True) if status in MISSING_STATUSES]
    if missing:
        log.info("environment_url_check_missing", missing=missing)
    return missing


def url_check_evidence(missing: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The reviewer prompt's daemon fact, empty when nothing is missing."""
    return {"daemon_url_checks": list(missing), "daemon_url_check_note": URL_CHECK_NOTE} if missing else {}


def unnamed_missing_commands(proposal: CoverageAssessmentProposal, missing: Sequence[Mapping[str, Any]],
                             records: Sequence[FindingRecord]) -> list[str]:
    """The commands with a binding missing URL that no new finding, and no assigned record decided open, names."""
    wanted = {identifier for item in missing if not item.get("reported_before") for identifier in item["command_ids"]}
    if not wanted:
        return []
    by_id = {record.id: record for record in records}
    named = {identifier for finding in proposal.findings for identifier in finding.affected_ids}
    named |= {identifier for decision in proposal.decisions if decision.status == "open" and decision.finding_id in by_id
              for identifier in by_id[decision.finding_id].proposal.affected_ids}
    return sorted(wanted - named)


def missing_url_error(unnamed: Sequence[str], missing: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A correctable review error: the reviewer must name each command whose URL the daemon found missing."""
    return {"code": "review_findings_invalid", "location": ["assessment", "findings"], "unnamed_command_ids": list(unnamed),
            "daemon_url_checks": [item for item in missing if set(item["command_ids"]) & set(unnamed)],
            "guidance": ("Each unnamed_command_ids command downloads a URL the daemon requested and found missing (HTTP 404 "
                         "or 410; see daemon_url_checks). Report an execution_definition finding whose affected_ids name "
                         "it, or keep open an assigned finding that names it, and return revise.")}


def require_missing_urls_named(proposal: CoverageAssessmentProposal, missing: Sequence[Mapping[str, Any]],
                               records: Sequence[FindingRecord]) -> None:
    """Raises a correctable review error when a command with a missing URL is not named by a finding."""
    if unnamed := unnamed_missing_commands(proposal, missing, records):
        error = missing_url_error(unnamed, missing)
        raise FindingValidationError(error["guidance"] + " Commands: " + ", ".join(unnamed), code=error["code"],
                                     location=tuple(error["location"]))
