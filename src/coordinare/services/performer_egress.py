"""449: the egress allowlist of a Docker performer, enforced outside its container.

A performer endpoint with ``egress_allowlist`` (and the default ``egress_mode: proxy``) runs
each ephemeral performer the way #447 runs a sandboxed QAre job:

1. ``docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true
   coordinare-egress-<id>``: no route out, and no host address on its bridge, so host
   services are not reachable through a gateway address either.
2. A proxy container of the same name, from the endpoint's image, running
   ``performer.egress_proxy`` as ``nobody`` on a read-only root, on the default bridge (its
   route out), then connected to that network. It relays only to the allowlisted hosts.
3. The performer, on that network only, with ``PERFORMER_EGRESS_PROXY`` naming the proxy, and
   no ``NET_ADMIN``. Its entry point waits for the proxy and points every client at it.

Docker publishes no port of a container whose only network is internal, so the proxy
publishes the performer's port and relays it (``--forward``) to the performer's network alias.
The proxy follows that alias (``--follow``) and exits once the performer is gone, however it
was stopped; its container is ``--rm``. ``DockerRuntime.stop`` removes both at once (a fixed
``port`` stays held by the proxy until then). Networks left behind are swept at each proxied start:
``docker network rm`` refuses a network that still has a container on it, so a running
performer's network is never removed, and a grace keeps a network another start has only just
made. Neither the network nor the proxy carries ``coordinare.performer.id``, so reconciliation
and the orphan sweep never take the proxy for a performer.

Any failure while setting up removes what was made and raises :class:`EgressSetupError`; the
performer is then never started, so it never runs with a route out the allowlist does not
cover.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from coordinare.services.stage_jobs import (
    EGRESS_IMAGE_HINT,
    EGRESS_NETWORK_OPTIONS,
    EGRESS_PROXY_ENV,
    EGRESS_PROXY_MODULE,
    EGRESS_PROXY_PORT,
    NETWORK_SUBNETS_FORMAT,
    ipv4_subnet,
    network_address,
)

if TYPE_CHECKING:
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

logger = structlog.get_logger(__name__)

#: On the performer, its network and its proxy: the name both of them have.
EGRESS_LABEL = "coordinare.performer.egress"
#: On the network and the proxy: when they were made (seconds since the epoch).
EGRESS_CREATED_LABEL = "coordinare.performer.egress.created"
#: On the network and the proxy: the performer endpoint they were made for.
EGRESS_FOR_LABEL = "coordinare.performer.egress.for"
EGRESS_PREFIX = "coordinare-egress-"
#: The port the performer serves on, inside its container and on its proxy.
PERFORMER_PORT = 8088
#: A network this old with nothing on it is removed by the next proxied start.
SWEEP_GRACE_SECONDS = 600.0
#: Removing a performer's network right after it stopped: attempts, and the wait between them.
NETWORK_RM_ATTEMPTS = 3
_NETWORK_RM_RETRY_SECONDS = 1.0

#: ``performer_lifecycle._run_docker``: (exit code, stdout, stderr) of one docker command.
Docker = Callable[..., Awaitable[tuple[int, str, str]]]


class EgressSetupError(RuntimeError):
    """The performer's network or proxy could not be set up; nothing is left running."""


def proxied(config: PerformerEndpointConfig) -> bool:
    """Whether ``config``'s performers run behind an egress proxy."""
    return bool(config.egress_allowlist) and config.egress_mode == "proxy"


def new_name() -> str:
    """A fresh name for one performer's network and proxy."""
    return EGRESS_PREFIX + uuid.uuid4().hex[:12]


def alias(name: str) -> str:
    """The performer's network alias on network ``name``."""
    return f"{name}-performer"


def performer_args(name: str, proxy_url: str) -> list[str]:
    """What the performer's ``docker run`` gets instead of a published port and NET_ADMIN."""
    return [
        "--network", name, "--network-alias", alias(name),
        "--label", f"{EGRESS_LABEL}={name}",
        "-e", f"{EGRESS_PROXY_ENV}={proxy_url}",
    ]


async def up(docker: Docker, config: PerformerEndpointConfig, name: str, image: str) -> str:
    """Make the network and proxy called ``name``; the proxy's URL on that network."""
    with contextlib.suppress(Exception):
        await sweep(docker)
    labels = [
        "--label", f"{EGRESS_LABEL}={name}",
        "--label", f"{EGRESS_CREATED_LABEL}={int(time.time())}",
        "--label", f"{EGRESS_FOR_LABEL}={config.id}",
    ]
    hosts: list[str] = []
    for entry in config.extra_hosts:
        hosts += ["--add-host", entry]
    # An absolute name (trailing dot): no resolver search domain can make it an outside host.
    target = alias(name) + "."
    try:
        await _step(docker, "create its internal network", "network", "create", "--internal",
                    *EGRESS_NETWORK_OPTIONS, *labels, name)
        subnet = await _subnet(docker, name)
        await _step(
            docker, "start its proxy",
            "run", "-d", "--rm", "--name", name, *labels,
            "--user", "65534:65534", "--read-only", *hosts,
            "-p", f"{config.bind_host}:{config.port or 0}:{PERFORMER_PORT}",
            "--entrypoint", "python", image,
            "-m", EGRESS_PROXY_MODULE, "--allow", ",".join(config.egress_allowlist or []),
            "--port", str(EGRESS_PROXY_PORT),
            "--forward", f"{PERFORMER_PORT}={target}:{PERFORMER_PORT}", "--follow", target,
            # Only addresses on the performer's network: an answer for the alias from anywhere
            # else is never dialled, so the daemon's requests cannot be relayed out.
            "--within", subnet,
        )
        await _step(
            docker, f"attach its proxy to the internal network ({EGRESS_IMAGE_HINT})",
            "network", "connect", name, name,
        )
        address = await _proxy_address(docker, name)
    except BaseException:
        await down(docker, name)
        raise
    logger.info("performer_egress.proxy_up", performer_id=config.id, network=name, hosts=list(config.egress_allowlist or []))
    return f"http://{address}:{EGRESS_PROXY_PORT}"


async def down(docker: Docker, name: str) -> None:
    """Remove the proxy, then the network. Best effort: the next sweep catches what is left.

    ``docker stop`` returns before an ``--rm`` performer is removed, so its network can still be
    in use for a moment: removing it is tried a few times.
    """
    with contextlib.suppress(Exception):
        await docker("rm", "-f", name)
    for attempt in range(NETWORK_RM_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_NETWORK_RM_RETRY_SECONDS)
        try:
            code, _out, _err = await docker("network", "rm", name)
        except Exception:  # best effort, as above
            continue
        if code == 0:
            return


async def egress_of(docker: Docker, container_id: str) -> str:
    """The name of a performer's network and proxy, from its label; "" when it has none."""
    try:
        code, out, _err = await docker(
            "container", "inspect", "--format", f'{{{{index .Config.Labels "{EGRESS_LABEL}"}}}}', container_id,
        )
    except Exception:  # a performer whose label cannot be read is stopped all the same
        return ""
    name = out.strip() if code == 0 else ""
    return name if name.startswith(EGRESS_PREFIX) else ""


async def sweep(docker: Docker) -> None:
    """Remove egress networks older than the grace. Docker refuses any still in use."""
    code, out, _err = await docker(
        "network", "ls", "--filter", f"label={EGRESS_LABEL}",
        "--format", f'{{{{.Name}}}}\t{{{{.Label "{EGRESS_CREATED_LABEL}"}}}}',
    )
    if code != 0:
        return
    now = time.time()
    for line in out.splitlines():
        network, _tab, created = line.strip().partition("\t")
        if not network.startswith(EGRESS_PREFIX):
            continue
        try:
            if now - float(created) < SWEEP_GRACE_SECONDS:
                continue
        except ValueError:
            pass
        code, _out, _err = await docker("network", "rm", network)
        if code == 0:
            logger.info("performer_egress.swept", network=network)


async def _subnet(docker: Docker, name: str) -> str:
    out = await _step(docker, "read its network's subnet", "network", "inspect", "--format", NETWORK_SUBNETS_FORMAT, name)
    subnet = ipv4_subnet(out)
    if not subnet:
        msg = f"could not set up a performer's egress proxy: its network {name} has no subnet"
        raise EgressSetupError(msg)
    return subnet


async def _proxy_address(docker: Docker, name: str) -> str:
    inspected = await _step(
        docker, "read its proxy's address",
        "container", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name,
    )
    address = network_address(inspected, name)
    if not address:
        msg = f"could not set up a performer's egress proxy: its proxy has no address on {name}"
        raise EgressSetupError(msg)
    return address


async def _step(docker: Docker, what: str, *args: str) -> str:
    code, out, err = await docker(*args)
    if code != 0:
        msg = f"could not set up a performer's egress proxy: docker could not {what}: {(err or out).strip()[-500:]}"
        raise EgressSetupError(msg)
    return out
