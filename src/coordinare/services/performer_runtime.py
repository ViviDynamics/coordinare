"""Runtime-agnostic performer lifecycle (spec 146 / issue #200).

Coordinare starts an ephemeral performer, talks to it over HTTP, and stops it.
Until now the code that decided *how* to start one was the same code that started
it with Docker: ``http_performer_service`` imported ``performer_lifecycle``
directly. This module is the seam that lets a second runtime exist.

**Why this is not in ``transport/``.** ``transport.base.AgentTransport`` is the
*subprocess wire protocol* — a single ``send(message) -> response``. HTTP
performers never use it. Issue #200 described the Kubernetes work as "implement
the ``KubernetesTransport`` stub" precisely because that stub sat in
``transport/`` and looked like the extension point. It was not, and it is deleted.

**What is deliberately NOT here: ``wait_ready``.** It polls the performer's
``GET /status`` over HTTP and is already runtime-agnostic, so it stays a shared
free function in ``performer_lifecycle``. Only the three operations that genuinely
differ per runtime are abstracted. Widening a Protocol beyond what actually varies
makes every implementation carry code it did not need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from coordinare.models.performer_endpoint import PerformerEndpointConfig


@dataclass(frozen=True, slots=True)
class StartedPerformer:
    """A performer that has been started and can be reached.

    Deliberately not named ``StartedContainer``. Kubernetes does not return a
    container, and carrying the word forward is how the next reader concludes
    this abstraction is Docker-shaped.
    """

    #: Opaque runtime handle. A container id under Docker, a Pod name under
    #: Kubernetes. Only the runtime that produced it may interpret it.
    handle: str

    #: Where to reach the performer's HTTP service, e.g.
    #: ``http://127.0.0.1:8080`` (Docker) or ``http://10.244.0.5:8088``
    #: (Kubernetes). Everything downstream consumes only this, which is why the
    #: HTTP client needs no changes to support a second runtime.
    endpoint: str


@runtime_checkable
class PerformerRuntime(Protocol):
    """How to start, stop, and reap performers for one runtime.

    Implementations must be safe to call concurrently for distinct performers:
    coordinare runs several cards at once.
    """

    async def start_ephemeral(
        self,
        config: PerformerEndpointConfig,
        *,
        extra_labels: dict[str, str] | None = None,
    ) -> StartedPerformer:
        """Start a fresh performer and return its handle and endpoint.

        Returns once the performer is *addressable*, not once it is *ready* —
        the caller then polls readiness over HTTP. Under Docker that means the
        container is running and its port is published; under Kubernetes, that
        the Pod is running and has an IP.

        Raises a runtime-specific error deriving from
        ``performer_lifecycle.LifecycleError`` when the performer cannot be
        started, so existing ``except`` clauses keep working across runtimes.
        """
        ...

    async def stop(
        self,
        handle: str,
        *,
        timeout_s: int = 10,
        host_log_dir: Path | None = None,
        performer_id: str | None = None,
    ) -> None:
        """Stop and remove a performer. Best-effort: failures are logged, not raised.

        When *host_log_dir* is set, the performer's logs MUST be captured
        **before** it is removed. Under Kubernetes this is not a nicety: Pod logs
        are unreachable the moment the Pod is deleted, so the ordering is the
        requirement rather than an optimisation.
        """
        ...

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        """Most recent log lines from a *running* performer, newest last.

        Distinct from the log capture in ``stop``: that writes a final artefact to
        disk as the performer is torn down, while this feeds the dashboard's live
        view of a job still in flight.

        It belongs in the Protocol because it is genuinely runtime-specific —
        ``docker logs`` against a container id, the Pod log endpoint against a Pod
        name. Reaching for ``docker logs`` directly is how a caller silently
        stopped working under Kubernetes, where the handle is a Pod name and the
        command fails on every poll while the buffer just stays empty.

        Best-effort: returns an empty list rather than raising, since a missing
        live log is not a reason to fail the job it belongs to.
        """
        ...

    async def cleanup_orphaned(self, performer_id: str | None = None) -> int:
        """Reap performers left behind by a previous coordinare crash.

        Returns how many were stopped. *performer_id* narrows the sweep to one
        endpoint's performers; an unscoped sweep on a shared host or namespace
        would also reap another coordinare's running work.
        """
        ...
