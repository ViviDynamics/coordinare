"""Resolve performer image tags to registry digests for pinning (#526).

A mutable performer tag (``coordinare-performer:full``) is resolved to its
current registry digest when a container or Pod is dispatched, and the
dispatch is pinned to ``name@sha256:...`` instead. Node-cache behavior is
unchanged (FR-013): a digest ref is either already cached on the node or
pulled once by digest — never a fresh 4 GB pull per dispatch.

Resolution failure fails loud: dispatch raises rather than silently falling
back to the mutable tag.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from coordinare.services.performer_lifecycle import LifecycleError, _run_docker

logger = structlog.get_logger(__name__)

#: Subprocess timeout for the registry manifest lookup. Network registries can
#: be slow; a hung lookup must not block the dispatch loop forever, but the
#: budget is deliberately larger than container-start's 30 s.
INSPECT_TIMEOUT_SECONDS = 120.0

_INSPECT_FORMAT = "{{.Manifest.Digest}}"

Runner = Callable[..., Awaitable[tuple[int, str, str]]]


class DigestResolutionError(LifecycleError):
    """The registry digest for a performer image tag could not be resolved."""


def parse_image_ref(image: str) -> tuple[str, str]:
    """Split *image* into ``(name, tag)``.

    Handles bare names (``coordinare-performer`` → latest), tags
    (``coordinare-performer:full``), registry hosts with ports
    (``reg.local:5000/name:tag``), and registry hosts without ports
    (``ghcr.io/org/name:tag``). Digest refs are not split — callers check for
    ``@`` first.
    """
    last_slash = image.rfind("/")
    colon = image.find(":", last_slash + 1)
    if colon == -1:
        return image, "latest"
    return image[:colon], image[colon + 1 :]


class PerformerImageResolver:
    """Resolve ``tag`` → ``digest`` for performer images, with a slow-clock cache.

    *runner* is injectable for tests; it mirrors ``performer_lifecycle._run_docker``
    (returning ``(rc, stdout, stderr)``).
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        refresh_seconds: int = 300,
        runner: Any = None,
        clock: Any = None,
    ) -> None:
        self._enabled = enabled
        self._refresh_seconds = refresh_seconds
        self._runner: Runner = runner if runner is not None else _run_docker
        self._clock = clock if clock is not None else time.monotonic
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def pin(self, image: str) -> str:
        """Return *image*, pinned to a digest when resolution is enabled.

        Digest refs pass through unchanged; tags are resolved to
        ``<name>@sha256:<digest>`` and cached per tag for ``refresh_seconds``.
        """
        if not self._enabled:
            return image
        if "@" in image:
            return image
        name, _tag = parse_image_ref(image)
        now = self._clock()
        cached = self._cache.get(image)
        if cached is not None and (now - cached[1]) < self._refresh_seconds:
            return f"{name}@{cached[0]}"

        async with self._lock:
            # Double-check under the lock: a concurrent pin may have resolved.
            cached = self._cache.get(image)
            if cached is not None and (self._clock() - cached[1]) < self._refresh_seconds:
                return f"{name}@{cached[0]}"

            digest = await self._resolve(image)
            self._cache[image] = (digest, self._clock())
            logger.info("image_digest.resolved", image=image, digest=digest)
            return f"{name}@{digest}"

    async def _resolve(self, image: str) -> str:
        rc, stdout, stderr = await self._runner(
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            _INSPECT_FORMAT,
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        if rc != 0:
            msg = (
                f"could not resolve registry digest for {image!r} "
                f"(rc={rc}): {stderr or stdout}"
            )
            raise DigestResolutionError(msg)
        trimmed = stdout.strip()
        digest = trimmed.splitlines()[-1].strip() if trimmed else ""
        if not digest.startswith("sha256:"):
            msg = f"unexpected imagetools output for {image!r}: {stdout!r}"
            raise DigestResolutionError(msg)
        return digest
