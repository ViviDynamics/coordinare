"""Preflight checks for a coordinare deployment (spec 145 / issue #199).

The failure this exists to prevent: an operator discovers a missing token
permission or an unreachable endpoint when a card fails halfway through a run,
having already spent model budget, and reasonably concludes coordinare is broken
rather than unconfigured.

Every check answers one question an operator can act on, and every failure names
the fix. A check that reports "something is wrong" without saying what to change
has moved the problem rather than found it.

``config validate`` proves the file parses. This proves the world it describes
actually exists. Neither substitutes for the other: a config can be structurally
perfect and point at a host that is not listening.
"""

from __future__ import annotations

import os
import re
from base64 import b64encode
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from coordinare.doctor_status import Status
from coordinare.lib.transcript_redaction import redact_text
from coordinare.metrics import _coordinare_version
from coordinare.network_probe import error_status, probe_http

__all__ = [
    "PROBE_TIMEOUT_SECONDS", "CheckResult", "DoctorReport", "Status", "check_dashboard_binding",
    "check_endpoints", "check_kubernetes_egress_enforcement", "check_models", "run_checks",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coordinare.config import Endpoint, ProjectConfiguration

#: How long to wait for an endpoint before calling it unreachable. Short on
#: purpose: this is a setup gate, and an operator waiting 30 seconds per endpoint
#: to be told something is wrong will stop running it.
PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One preflight answer.

    ``fix`` is required for a failure and is the whole point of the check.
    """

    name: str
    status: Status
    detail: str
    fix: str | None = None

    def __post_init__(self) -> None:
        if self.status is Status.FAIL and not self.fix:
            msg = f"check {self.name!r} failed without naming a fix"
            raise ValueError(msg)


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        icons = {Status.OK: "PASS", Status.FAIL: "FAIL", Status.WARN: "WARN", Status.SKIP: "SKIP"}
        lines = ["", f"coordinare preflight ({_coordinare_version()})", "=" * 60]
        for r in self.results:
            lines.append(f"  [{icons[r.status]}] {r.name}")
            lines.append(f"         {r.detail}")
            if r.fix:
                lines.append(f"         fix: {r.fix}")
        lines.append("=" * 60)
        if self.ok:
            lines.append(
                "No blocking failures. Review the warnings above; Coordinare can start."
                if any(result.status is Status.WARN for result in self.results)
                else "All checks passed. Coordinare should be able to start.",
            )
            lines.append("")
            lines.append("These checks cover reachability, configuration and permissions.")
            lines.append("Warnings identify what could not be verified. These checks cannot prove")
            lines.append("model output quality; that is only known after the model runs.")
        else:
            lines.append(f"{len(self.failed)} check(s) failed. Each names its fix above.")
        lines.append("")
        return "\n".join(lines)


def _uses_ollama_tags(base_url: str, kind: str | None) -> bool:
    return kind == "ollama" and not httpx.URL(base_url).path.rstrip("/").endswith("/v1")


def _models_url(base_url: str, kind: str | None = None) -> str:
    url = httpx.URL(base_url)
    route = "/api/tags" if _uses_ollama_tags(base_url, kind) else "/models"
    return str(url.copy_with(path=url.path.rstrip("/") + route, username="", password="", fragment=None))


def _endpoint_headers(endpoint: Endpoint) -> dict[str, str]:
    credential = os.environ.get(getattr(endpoint, "auth_env", None) or "", "")
    if credential:
        return {"Authorization": "Bearer " + credential}
    url = httpx.URL(endpoint.base_url or "")
    if url.username or url.password:
        encoded = b64encode(f"{url.username}:{url.password}".encode()).decode("ascii")
        return {"Authorization": "Basic " + encoded}
    return {}


def _safe_endpoint_text(text: str, endpoint: Endpoint) -> str:
    """Redact known credentials and token patterns before rendering diagnostics."""
    url = httpx.URL(endpoint.base_url or "")
    secrets = [url.username, url.password, *url.params.values()]
    secrets.append(_endpoint_headers(endpoint).get("Authorization", "").partition(" ")[2])
    # Known credentials must be hidden even when shorter than the general
    # transcript redactor's minimum exact-secret length.
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(://)[^/\s@]+@", r"\1", text)
    return redact_text(text)


def _probe_endpoint(
    base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS, *, headers: dict[str, str] | None = None,
    kind: str | None = None,
) -> tuple[Status, str]:
    try:
        # Credentials belong to this endpoint. Never follow a redirect to a
        # different host (including when a client's default enables redirects).
        response = probe_http(lambda: httpx.get(
            _models_url(base_url, kind), headers=headers, timeout=timeout, follow_redirects=False,
        ))
    except httpx.HTTPError as exc:
        return error_status(exc), "could not reach the endpoint after bounded retries"
    status = Status.OK if response.is_success else Status.WARN
    return status, f"responded with HTTP {response.status_code}"


def _list_models(
    base_url: str, headers: dict[str, str], timeout: float = PROBE_TIMEOUT_SECONDS, *, kind: str | None = None,
) -> list[str] | None:
    """Return served ids, including an empty catalog, or None if unavailable."""
    try:
        response = probe_http(lambda: httpx.get(
            _models_url(base_url, kind), headers=headers, timeout=timeout, follow_redirects=False,
        ))
        if not response.is_success:
            return None
        payload = response.json()
        catalog_key, id_key = ("models", "name") if _uses_ollama_tags(base_url, kind) else ("data", "id")
        if not isinstance(payload, dict) or not isinstance(payload.get(catalog_key), list):
            return None
        models = payload[catalog_key]
        if any(not isinstance(model, dict) or not isinstance(model.get(id_key), str) for model in models):
            return None
        return [model[id_key] for model in models]
    except (httpx.HTTPError, ValueError):
        return None


def check_endpoints(config: ProjectConfiguration) -> list[CheckResult]:
    """FR-009 — is each configured model endpoint actually reachable?"""
    results: list[CheckResult] = []
    for endpoint in config.endpoints:
        base_url = getattr(endpoint, "base_url", None)
        if not base_url:
            results.append(
                CheckResult(
                    name=f"endpoint '{endpoint.name}'",
                    status=Status.SKIP,
                    detail=(
                        f"kind '{endpoint.kind}' is a native provider with no base_url; "
                        "reachability is checked when a request is made"
                    ),
                ),
            )
            continue

        status, detail = _probe_endpoint(base_url, headers=_endpoint_headers(endpoint), kind=endpoint.kind)
        safe_url = str(httpx.URL(base_url).copy_with(username="", password="", query=None, fragment=None))
        route = _safe_endpoint_text(_models_url(safe_url, endpoint.kind), endpoint)
        route_hint = "" if _uses_ollama_tags(base_url, endpoint.kind) else "base_url usually ends in /v1. "
        results.append(
            CheckResult(
                name=f"endpoint '{endpoint.name}'",
                status=status,
                detail=f"{route}: {detail}",
                fix=(
                    None
                    if status is Status.OK
                    else (
                        f"Verify GET {route}; {route_hint}"
                        "Check credentials, start the service, or correct base_url for endpoint "
                        f"'{endpoint.name}'. If coordinare runs in Docker and the endpoint is on "
                        "the host, use host.docker.internal rather than localhost."
                    )
                ),
            ),
        )
    return results


def check_models(config: ProjectConfiguration) -> list[CheckResult]:
    """FR-011 — does the endpoint serve the model that is configured?

    On a mismatch the check lists what the endpoint *does* serve, because that is
    almost always the fix: the name is misspelled, or the model was never pulled.
    """
    by_name = {e.name: e for e in config.endpoints}
    results: list[CheckResult] = []

    for model_endpoint in config.model_endpoints:
        endpoint = by_name.get(model_endpoint.endpoint)
        base_url = getattr(endpoint, "base_url", None) if endpoint else None
        headers = _endpoint_headers(endpoint) if endpoint else {}
        if not base_url and endpoint and endpoint.kind == "openai" and headers:
            base_url = "https://api.openai.com/v1"
        available = _list_models(base_url, headers, kind=endpoint.kind) if base_url and endpoint else None
        if available is None or endpoint is None:
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.SKIP,
                    detail="Model enumeration is unavailable; the configured model is checked on first use.",
                ),
            )
            continue

        if model_endpoint.model in available:
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.OK,
                    detail=_safe_endpoint_text(
                        f"'{model_endpoint.model}' is served by '{model_endpoint.endpoint}'", endpoint,
                    ),
                ),
            )
        else:
            shown = ", ".join(_safe_endpoint_text(model, endpoint)[:80] for model in sorted(available)[:8]) or "(none)"
            more = "" if len(available) <= 8 else f" (+{len(available) - 8} more)"
            load_hint = (
                _safe_endpoint_text(f"pull it with `ollama pull {model_endpoint.model}`", endpoint)
                if endpoint.kind == "ollama" else "load the configured model"
            )
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.WARN,
                    detail=_safe_endpoint_text(
                        f"'{model_endpoint.model}' is not served by '{model_endpoint.endpoint}'", endpoint,
                    ),
                    fix=(
                        f"Set model_endpoints[].model to a served id, or {load_hint}. "
                        f"Served ids: {shown}{more}"
                    ),
                ),
            )
    return results


def check_dashboard_binding(config: ProjectConfiguration) -> list[CheckResult]:
    """The dashboard has no authentication (spec 144). Say so if it is exposed."""
    from coordinare.localhost_guard import is_loopback_bind

    host = config.dashboard_host
    if is_loopback_bind(host):
        return [
            CheckResult(
                name="dashboard binding",
                status=Status.OK,
                detail=f"loopback ({host}); not reachable from the network",
            ),
        ]
    return [
        CheckResult(
            name="dashboard binding",
            status=Status.WARN,
            detail=(
                f"bound to {host}, which is NOT loopback. The dashboard has no "
                "authentication: anyone who can reach it can change configuration, "
                "cancel work, and delete symphonies."
            ),
            fix=(
                "set dashboard_host to 127.0.0.1 unless the network is fully trusted. If this "
                "is deliberate, add the hostname you browse to into trusted_dashboard_hosts, or "
                "the localhost guard will refuse every request with 403. "
                "See docs/security/threat-model.md."
            ),
        ),
    ]


def check_performer_versions(config: ProjectConfiguration) -> list[CheckResult]:
    installed = _coordinare_version()
    results = []
    for endpoint in getattr(config, "performer_endpoints", []):
        if endpoint.mode != "ephemeral":
            continue
        image = endpoint.image or config.performer_image
        tagged = image.split("@", 1)[0]
        tag = tagged.rsplit("/", 1)[-1].partition(":")[2]
        matches = tag == installed
        results.append(CheckResult(
            "performer version " + endpoint.id,
            Status.OK if matches else Status.WARN,
            "Performer image tag matches the daemon version." if matches else
            f"Performer image tag differs from daemon version {installed} or is missing.",
            None if matches else "After upgrading Coordinare, rerun coordinare init --force --run-mode docker to pull and pin its release image; use --image-tag VERSION only for an intentional override.",
        ))
    return results


def run_checks(config: ProjectConfiguration) -> DoctorReport:
    """Run every preflight check and collect the results."""
    report = DoctorReport()
    report.results.extend(check_performer_versions(config))
    report.results.extend(check_endpoints(config))
    report.results.extend(check_models(config))
    report.results.extend(check_dashboard_binding(config))
    return report


def check_kubernetes_egress_enforcement(config: ProjectConfiguration) -> list[CheckResult]:
    """Issue #225 — does this cluster actually enforce NetworkPolicy egress?

    Opt-in (``--check egress``) rather than part of every run, because unlike the
    other checks this one **creates Pods**. A preflight that quietly schedules
    workloads every time somebody runs it would be a surprise.

    It runs the experiment rather than inferring from the CNI's name. Which CNI is
    installed is a poor proxy: support is frequently *partial*, and measured on
    kind, kindnet enforces ingress but not egress. A guess in the optimistic
    direction tells an operator they are contained when they are not.
    """
    import asyncio

    if config.agent_transport != "kubernetes":
        return [
            CheckResult(
                name="egress enforcement",
                status=Status.SKIP,
                detail=(
                    f"agent_transport is '{config.agent_transport}'; egress on the "
                    "Docker path is enforced in-container with iptables, not by a "
                    "NetworkPolicy"
                ),
            ),
        ]

    try:
        from kubernetes import client
        from kubernetes import config as kube_config

        from coordinare.services.kubernetes_egress import probe_network_policy_enforcement
    except ImportError as exc:  # pragma: no cover - defensive
        return [
            CheckResult(
                name="egress enforcement",
                status=Status.FAIL,
                detail=f"the Kubernetes client is unavailable: {exc}",
                fix="install coordinare with its Kubernetes extra",
            ),
        ]

    image = next(
        (ep.image for ep in config.performer_endpoints if getattr(ep, "image", None)), None,
    )
    if not image:
        return [
            CheckResult(
                name="egress enforcement",
                status=Status.SKIP,
                detail="no performer endpoint declares an image, so there is nothing to probe with",
            ),
        ]

    try:
        kube_config.load_incluster_config()
    except Exception:
        try:
            kube_config.load_kube_config()
        except Exception as exc:
            return [
                CheckResult(
                    name="egress enforcement",
                    status=Status.FAIL,
                    detail=f"no usable Kubernetes credentials: {exc}",
                    fix="set KUBECONFIG, or run this inside the cluster",
                ),
            ]

    verdict = asyncio.run(
        probe_network_policy_enforcement(
            core_v1=client.CoreV1Api(),
            networking_v1=client.NetworkingV1Api(),
            namespace=config.kubernetes_namespace,
            image=image,
        ),
    )

    if not verdict.conclusive:
        return [
            CheckResult(
                name="egress enforcement",
                status=Status.WARN,
                detail=verdict.detail,
                fix=(
                    "re-run when the cluster can schedule Pods in "
                    f"'{config.kubernetes_namespace}'. Until this answers, do not assume "
                    "performers are contained."
                ),
            ),
        ]

    if verdict.enforced:
        return [
            CheckResult(
                name="egress enforcement",
                status=Status.OK,
                detail=verdict.detail,
            ),
        ]

    return [
        CheckResult(
            name="egress enforcement",
            status=Status.WARN,
            detail=verdict.detail,
            fix=(
                "leave performers.egress.enabled off, and do not treat performers as "
                "network-restricted here. To gain the restriction, move to a CNI that "
                "enforces egress policy (Calico, Cilium) or place an egress proxy in "
                "front of them."
            ),
        ),
    ]
