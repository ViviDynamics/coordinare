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

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coordinare.config import ProjectConfiguration

#: How long to wait for an endpoint before calling it unreachable. Short on
#: purpose: this is a setup gate, and an operator waiting 30 seconds per endpoint
#: to be told something is wrong will stop running it.
PROBE_TIMEOUT_SECONDS = 5.0


class Status(StrEnum):
    OK = "ok"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


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
        lines = ["", "coordinare preflight", "=" * 60]
        for r in self.results:
            lines.append(f"  [{icons[r.status]}] {r.name}")
            lines.append(f"         {r.detail}")
            if r.fix:
                lines.append(f"         fix: {r.fix}")
        lines.append("=" * 60)
        if self.ok:
            lines.append("All checks passed. Coordinare should be able to start.")
            lines.append("")
            lines.append("Note this proves your endpoints answer and your token has the")
            lines.append("permissions checked here. It cannot prove a model produces good")
            lines.append("output, only that it exists and responds.")
        else:
            lines.append(f"{len(self.failed)} check(s) failed. Each names its fix above.")
        lines.append("")
        return "\n".join(lines)


def _probe_endpoint(base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    try:
        response = httpx.get(base_url, timeout=timeout, follow_redirects=True)
    except httpx.ConnectError as exc:
        return False, f"cannot connect: {exc}"
    except httpx.TimeoutException:
        return False, f"no response within {timeout:g}s"
    except httpx.HTTPError as exc:  # pragma: no cover - defensive
        return False, f"request failed: {exc}"
    # Any HTTP answer means something is listening, which is the question asked.
    # A 404 from a base URL is normal and not a problem.
    return True, f"responded with HTTP {response.status_code}"


def _list_ollama_models(base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> list[str]:
    """Model names an Ollama endpoint serves, or an empty list if unavailable."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        response.raise_for_status()
        return [m["name"] for m in response.json().get("models", []) if "name" in m]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


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

        reachable, detail = _probe_endpoint(base_url)
        results.append(
            CheckResult(
                name=f"endpoint '{endpoint.name}'",
                status=Status.OK if reachable else Status.FAIL,
                detail=f"{base_url} — {detail}",
                fix=(
                    None
                    if reachable
                    else (
                        f"start the service at {base_url}, or correct base_url for endpoint "
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
        if not endpoint or not base_url or endpoint.kind != "ollama":
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.SKIP,
                    detail=(
                        f"'{model_endpoint.model}' cannot be enumerated for endpoint kind "
                        f"'{getattr(endpoint, 'kind', 'unknown')}'; it is checked on first use"
                    ),
                ),
            )
            continue

        available = _list_ollama_models(base_url)
        if not available:
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.WARN,
                    detail=f"could not list models at {base_url}; skipping the name check",
                ),
            )
            continue

        if model_endpoint.model in available:
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.OK,
                    detail=f"'{model_endpoint.model}' is served by '{endpoint.name}'",
                ),
            )
        else:
            shown = ", ".join(sorted(available)[:8]) or "(none)"
            more = "" if len(available) <= 8 else f" (+{len(available) - 8} more)"
            results.append(
                CheckResult(
                    name=f"model '{model_endpoint.name}'",
                    status=Status.FAIL,
                    detail=f"'{model_endpoint.model}' is not served by '{endpoint.name}'",
                    fix=(
                        f"pull it with `ollama pull {model_endpoint.model}`, or set the model to "
                        f"one this endpoint already serves: {shown}{more}"
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


def run_checks(config: ProjectConfiguration) -> DoctorReport:
    """Run every preflight check and collect the results."""
    report = DoctorReport()
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
