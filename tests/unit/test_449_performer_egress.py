"""449: the egress allowlist is binding for every Docker performer, not only the QAre sandbox.

An ephemeral performer whose endpoint has ``egress_allowlist`` runs on a Docker network of its
own created with ``--internal`` (no route out), with a proxy container
(``performer.egress_proxy``) on that network and on the default bridge as its only exit. The
proxy also publishes the performer's port, since Docker publishes none for a container whose
only network is internal. Nothing here runs Docker: a fake ``docker`` records every argument
list, and the network is modelled from those lists at the rule level, as in
``test_447_sandbox_egress.py``.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from performer.egress_proxy import allowed, parse_allowlist, parse_forward

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import performer_egress
from coordinare.services.docker_executor import DockerExecutor
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.performer_egress import (
    EGRESS_CREATED_LABEL,
    EGRESS_FOR_LABEL,
    EGRESS_LABEL,
    SWEEP_GRACE_SECONDS,
)
from coordinare.services.performer_lifecycle import ContainerStartError, start_ephemeral
from coordinare.services.performer_lifecycle import _run_docker as _lifecycle_docker
from coordinare.services.stage_jobs import (
    EGRESS_PROXY_ENV,
    EGRESS_PROXY_MODULE,
    EGRESS_SUFFIX,
    DockerStageJobs,
    StageJobLaunch,
    job_name,
)
from coordinare.services.stage_providers import stage_jobs_for
from coordinare.stage_provider_config import QAreProviderConfig
from coordinare.transport.base import TransportError

IMAGE = "coordinare-performer:full"
ALLOW = ["api.github.com", "llm", "registry.npmjs.org"]
PROXY_IP = "198.51.100.2"
PROXY_PORT = "49200"

_FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
def fail(key, code=1):
    if state.get(key):
        sys.stderr.write(state[key] + "\n")
        sys.exit(code)
if args[:2] == ["network", "create"]:
    fail("network_create_fails")
    print("net0")
elif args[:2] == ["network", "connect"]:
    fail("connect_fails")
elif args[:2] == ["network", "rm"]:
    fail("network_rm_fails")
elif args[:2] == ["network", "ls"]:
    print(state.get("network_ls", ""))
elif args[:2] == ["network", "inspect"]:
    print(state.get("subnet", "198.51.100.0/24 "))
elif args[:1] == ["run"] and "--entrypoint" in args:
    fail("proxy_fails", 125)
    print("proxy0")
elif args[:1] == ["run"]:
    fail("performer_fails", 125)
    print("perf0")
elif args[:2] == ["container", "inspect"] and "{{.State.Running}}" in args:
    running = state.get("running", "missing")
    if running == "missing":
        sys.stderr.write("Error: No such container: perf0\n")
        sys.exit(1)
    print(running)
elif args[:2] == ["container", "inspect"] and "{{json .NetworkSettings.Networks}}" in args:
    name = args[-1]
    address = state.get("proxy_ip", "198.51.100.2")
    print(json.dumps({name: {"IPAddress": address}, "bridge": {"IPAddress": "198.51.100.9"}}))
elif args[:2] == ["container", "inspect"]:
    print(state.get("label", ""))
elif args[:1] == ["port"]:
    if args[1] == "perf0" and not state.get("performer_published"):
        sys.stderr.write("Error: No public port '8088/tcp' published for perf0\n")
        sys.exit(1)
    fail("port_fails")
    print("127.0.0.1:" + state.get("port", "49200"))
elif args[:1] in (["stop"], ["rm"]):
    pass
'''


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}")
    script.chmod(0o755)
    state = tmp_path / "docker-state.json"
    log = tmp_path / "docker-log.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def set_state(**values: Any) -> None:
        state.write_text(json.dumps(values))

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    set_state()
    return {"set": set_state, "calls": calls}


def _config(**overrides: Any) -> PerformerEndpointConfig:
    base: dict[str, Any] = {
        "id": "implementer-1",
        "mode": "ephemeral",
        "roles": ["implementer", "reviewer", "qa"],
        "image": IMAGE,
        "egress_allowlist": ALLOW,
        "extra_hosts": ["llm:192.0.2.10"],
        "env": {"OPENAI_BASE_URL": "http://llm:4000/v1"},
    }
    base.update(overrides)
    return PerformerEndpointConfig.model_validate(base)


async def _start(docker: dict[str, Any], config: PerformerEndpointConfig | None = None, **state: Any) -> Any:
    docker["set"](**state)
    return await start_ephemeral(config or _config(), backend="claude_code")


def _is_proxy_run(args: list[str]) -> bool:
    return args[:1] == ["run"] and "--entrypoint" in args


def _is_performer_run(args: list[str]) -> bool:
    return args[:1] == ["run"] and "--entrypoint" not in args


def _option(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def _index(calls: list[list[str]], match: Any) -> int:
    return next(i for i, args in enumerate(calls) if match(args))


def _labels(args: list[str]) -> dict[str, str]:
    return dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")


# -- the network, modelled from the argument lists -----------------------------------------


def _topology(calls: list[list[str]]) -> tuple[dict[str, bool], dict[str, set[str]], dict[str, str]]:
    """Networks (name: internal?), containers (name: networks) and each proxy's allowlist.

    Docker's own networks are not internal. A container started without ``--network`` is on
    the default bridge. The performer has no ``--name``; it is known by its network alias.
    """
    networks: dict[str, bool] = {"bridge": False, "host": False}
    containers: dict[str, set[str]] = {}
    proxies: dict[str, str] = {}
    for args in calls:
        if args[:2] == ["network", "create"]:
            networks[args[-1]] = "--internal" in args
        elif args[:2] == ["network", "connect"]:
            containers.setdefault(args[3], set()).add(args[2])
        elif args[:1] == ["run"]:
            name = _option(args, "--name") or _option(args, "--network-alias") or "performer"
            network = _option(args, "--network") or "bridge"
            containers[name] = set() if network == "none" else {network}
            if _is_proxy_run(args):
                proxies[name] = _option(args, "--allow") or ""
    return networks, containers, proxies


def _can_reach(calls: list[list[str]], container: str, host: str) -> bool:
    """Whether code in ``container``, as root and whatever it does to its own firewall, can open
    a connection to ``host``.

    Nothing inside the container counts: a route out exists only through a network that is not
    internal (host state), and on an internal network the only peers are the containers on it,
    of which a proxy passes only what its allowlist allows (the proxy's own decision function).
    So ``iptables -F`` or ``-P OUTPUT ACCEPT`` in the container changes nothing here.
    """
    networks, containers, proxies = _topology(calls)
    attached = containers[container]
    if any(not networks.get(n, False) for n in attached):
        return True
    peers = {name for name, nets in containers.items() if name != container and nets & attached}
    return any(name in proxies and allowed(host, parse_allowlist(proxies[name])) for name in peers)


def _performer(calls: list[list[str]]) -> str:
    run = next(a for a in calls if _is_performer_run(a))
    return _option(run, "--network-alias") or "performer"


# -- root in the performer cannot get out ----------------------------------------------------


@pytest.mark.asyncio
async def test_root_in_the_performer_cannot_reach_a_host_outside_the_allowlist(docker: dict[str, Any]) -> None:
    await _start(docker)
    calls = docker["calls"]()
    performer = _performer(calls)
    assert not _can_reach(calls, performer, "blocked.example")
    assert not _can_reach(calls, performer, "evil-api.github.com")
    assert not _can_reach(calls, performer, "1.1.1.1")
    networks, containers, proxies = _topology(calls)
    (egress,) = proxies
    assert containers[performer] == {egress}
    assert networks[egress] is True
    assert containers[egress] == {"bridge", egress}  # the proxy is the one container with a route out


@pytest.mark.asyncio
async def test_the_performer_has_no_net_admin_and_no_firewall_of_its_own(docker: dict[str, Any]) -> None:
    """Root cannot even run iptables; enforcement is host state and another container."""
    await _start(docker)
    for args in docker["calls"]():
        assert "--cap-add" not in args
        assert "--privileged" not in args
        assert not any(a.startswith("PERFORMER_EGRESS_ALLOWLIST") for a in args)
        assert "host" not in (_option(args, "--network"), _option(args, "--net"))


@pytest.mark.asyncio
async def test_the_model_gateway_github_and_registries_are_reached_through_the_proxy(docker: dict[str, Any]) -> None:
    await _start(docker)
    calls = docker["calls"]()
    performer = _performer(calls)
    for host in ALLOW:
        assert _can_reach(calls, performer, host), host
    run = next(a for a in calls if _is_performer_run(a))
    assert f"{EGRESS_PROXY_ENV}=http://{PROXY_IP}:3128" in run
    assert "OPENAI_BASE_URL=http://llm:4000/v1" in run  # the gateway URL is unchanged; the proxy relays it
    proxy = next(a for a in calls if _is_proxy_run(a))
    assert "llm:192.0.2.10" in proxy  # the proxy resolves the gateway's name as the performer did


def test_the_model_sees_a_performer_on_the_default_bridge_as_unrestricted() -> None:
    """The model is not vacuous: today's performer (no internal network) reaches anything."""
    calls = [["run", "-d", "--rm", "-p", "127.0.0.1:0:8088", IMAGE]]
    assert _can_reach(calls, "performer", "blocked.example")


# -- order, and the arguments ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_network_and_proxy_are_up_before_the_performer_starts(docker: dict[str, Any]) -> None:
    """The performer's network namespace is born on the internal network: its entry point,
    its backend install and the agent never see a route out."""
    started = await _start(docker)
    calls = docker["calls"]()
    created = _index(calls, lambda a: a[:2] == ["network", "create"])
    proxy = _index(calls, _is_proxy_run)
    connected = _index(calls, lambda a: a[:2] == ["network", "connect"])
    address = _index(calls, lambda a: "{{json .NetworkSettings.Networks}}" in a)
    performer = _index(calls, _is_performer_run)
    port = _index(calls, lambda a: a[:1] == ["port"])
    assert created < proxy < connected < address < performer < port
    egress = calls[created][-1]
    assert egress.startswith("coordinare-egress-")
    assert "--internal" in calls[created]
    assert calls[created][calls[created].index("-o") + 1] == "com.docker.network.bridge.inhibit_ipv4=true"
    assert calls[connected][2:] == [egress, egress]
    assert calls[port][1:] == [egress, "8088/tcp"]  # the port is published on the proxy
    assert started.container_id == "perf0"
    assert started.endpoint == f"http://127.0.0.1:{PROXY_PORT}"
    assert sum(1 for a in calls if _is_performer_run(a)) == 1


@pytest.mark.asyncio
async def test_the_performer_gets_only_the_internal_network_and_keeps_the_rest_of_its_setup(docker: dict[str, Any]) -> None:
    await _start(docker, _config(volumes=[{"host_path": "/srv/cache", "container_path": "/devenv", "mode": "rw"}]))
    calls = docker["calls"]()
    run = next(a for a in calls if _is_performer_run(a))
    egress = next(a for a in calls if a[:2] == ["network", "create"])[-1]
    assert run.count("--network") == 1
    assert _option(run, "--network") == egress
    assert _option(run, "--network-alias") == f"{egress}-performer"
    assert _labels(run)[EGRESS_LABEL] == egress
    assert _labels(run)["coordinare.performer.id"] == "implementer-1"
    assert "-p" not in run  # nothing published by the performer: Docker would not, and the proxy does
    assert "llm:192.0.2.10" in run
    assert "BACKEND=claude_code" in run
    assert "/srv/cache:/devenv:rw" in run
    assert run[-1] == IMAGE


@pytest.mark.asyncio
async def test_the_proxy_runs_unprivileged_publishes_the_performer_port_and_follows_it(docker: dict[str, Any]) -> None:
    await _start(docker, _config(port=18088, bind_host="0.0.0.0"))
    calls = docker["calls"]()
    proxy = next(a for a in calls if _is_proxy_run(a))
    egress = next(a for a in calls if a[:2] == ["network", "create"])[-1]
    alias = f"{egress}-performer"
    assert _option(proxy, "--name") == egress
    assert _option(proxy, "--user") == "65534:65534"
    assert "--read-only" in proxy
    assert "--rm" in proxy
    assert _option(proxy, "-p") == "0.0.0.0:18088:8088"
    assert _option(proxy, "--entrypoint") == "python"
    at = proxy.index("--entrypoint")
    assert proxy[at + 2:at + 5] == [IMAGE, "-m", EGRESS_PROXY_MODULE]
    assert _option(proxy, "--allow") == ",".join(ALLOW)
    # An absolute name: a search domain can never turn it into an outside host.
    assert parse_forward(_option(proxy, "--forward") or "") == (8088, f"{alias}.", 8088)
    assert _option(proxy, "--follow") == f"{alias}."
    # Pinned to the performer's network: an outside answer for the alias is never dialled.
    assert _option(proxy, "--within") == "198.51.100.0/24"
    assert "--network" not in proxy  # the default bridge: its route out
    labels = _labels(proxy)
    assert labels[EGRESS_LABEL] == egress
    assert labels[EGRESS_FOR_LABEL] == "implementer-1"
    assert abs(int(labels[EGRESS_CREATED_LABEL]) - time.time()) < 60
    # Not a performer: reconciliation and the orphan sweep look for this label.
    assert "coordinare.performer.id" not in labels
    assert "-v" not in proxy
    assert "--env-file" not in proxy
    assert not any(a.startswith("PERFORMER_AUTH_TOKEN") for a in proxy)


# -- fail closed -----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ({"network_create_fails": "Error: pool overlaps"}, "create its internal network"),
        ({"proxy_fails": "Error: no such image"}, "start its proxy"),
        ({"connect_fails": "Error: network not found"}, "attach its proxy"),
        ({"proxy_ip": ""}, "no address"),
        ({"subnet": " "}, "no subnet"),
    ],
)
async def test_a_setup_failure_starts_no_performer_and_removes_what_was_made(
    docker: dict[str, Any], state: dict[str, str], reason: str,
) -> None:
    with pytest.raises(ContainerStartError, match=reason):
        await _start(docker, **state)
    calls = docker["calls"]()
    assert not any(_is_performer_run(a) for a in calls)
    egress = next(a for a in calls if a[:2] == ["network", "create"])[-1]
    assert ["rm", "-f", egress] in calls
    assert ["network", "rm", egress] in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [{"performer_fails": "Error: bad env"}, {"port_fails": "Error: gone"}])
async def test_a_performer_that_fails_to_start_or_publish_is_torn_down_with_its_proxy(
    docker: dict[str, Any], state: dict[str, str],
) -> None:
    with pytest.raises(ContainerStartError):
        await _start(docker, **state)
    calls = docker["calls"]()
    egress = next(a for a in calls if a[:2] == ["network", "create"])[-1]
    assert ["rm", "-f", egress] in calls
    assert ["network", "rm", egress] in calls
    if "port_fails" in state:
        assert any(a[:1] == ["stop"] and a[-1] == "perf0" for a in calls)


# -- cleanup ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_start_sweeps_old_egress_networks_and_docker_keeps_the_ones_in_use(docker: dict[str, Any]) -> None:
    """``docker network rm`` refuses a network with a container on it, so a live performer's
    network survives the sweep; the grace protects one a concurrent start has just made."""
    old = int(time.time() - SWEEP_GRACE_SECONDS - 5)
    young = int(time.time() - 30)
    listing = "\n".join([
        f"coordinare-egress-aaaaaaaaaaaa\t{old}",
        f"coordinare-egress-bbbbbbbbbbbb\t{young}",
        "coordinare-egress-cccccccccccc\tnot-a-time",
    ])
    await _start(docker, network_ls=listing)
    calls = docker["calls"]()
    sweep = _index(calls, lambda a: a[:2] == ["network", "ls"])
    assert f"label={EGRESS_LABEL}" in calls[sweep]
    assert sweep < _index(calls, lambda a: a[:2] == ["network", "create"])
    assert ["network", "rm", "coordinare-egress-aaaaaaaaaaaa"] in calls
    assert ["network", "rm", "coordinare-egress-bbbbbbbbbbbb"] not in calls
    assert ["network", "rm", "coordinare-egress-cccccccccccc"] in calls


@pytest.mark.asyncio
async def test_a_sweep_that_fails_does_not_stop_the_start(docker: dict[str, Any]) -> None:
    old = int(time.time() - SWEEP_GRACE_SECONDS - 5)
    started = await _start(docker, network_ls=f"coordinare-egress-aaaaaaaaaaaa\t{old}", network_rm_fails="in use")
    assert started.container_id == "perf0"


@pytest.mark.asyncio
async def test_a_restarted_daemon_finds_a_proxied_performer_port_on_its_proxy(docker: dict[str, Any]) -> None:
    """076 reconciliation adopts running performers by ``docker port``; it follows the label."""
    docker["set"](label="coordinare-egress-aaaaaaaaaaaa", port="49301")
    assert await DockerExecutor().port_of("perf0") == 49301
    calls = docker["calls"]()
    assert calls[0] == ["port", "perf0", "8088/tcp"]
    assert calls[-1] == ["port", "coordinare-egress-aaaaaaaaaaaa", "8088/tcp"]


@pytest.mark.asyncio
async def test_a_performer_without_a_port_or_a_proxy_is_still_unreachable(docker: dict[str, Any]) -> None:
    docker["set"](label="")
    assert await DockerExecutor().port_of("perf0") is None


@pytest.mark.asyncio
async def test_a_performer_that_publishes_its_own_port_needs_no_proxy_lookup(docker: dict[str, Any]) -> None:
    docker["set"](performer_published=True, port="49302")
    assert await DockerExecutor().port_of("perf0") == 49302
    assert len(docker["calls"]()) == 1


# -- without an allowlist, or with the opt-out, nothing changes ------------------------------


@pytest.mark.asyncio
async def test_a_performer_without_an_allowlist_starts_exactly_as_before(docker: dict[str, Any]) -> None:
    await _start(docker, _config(egress_allowlist=None), performer_published=True)
    calls = docker["calls"]()
    assert [a[0] for a in calls] == ["run", "port"]
    assert calls[0] == [
        "run", "-d", "--rm", "--label", "coordinare.performer.id=implementer-1",
        "--add-host", "llm:192.0.2.10",
        "-p", "127.0.0.1:0:8088",
        "-e", "OPENAI_BASE_URL=http://llm:4000/v1",
        "-e", "BACKEND=claude_code",
        IMAGE,
    ]
    assert calls[1] == ["port", "perf0", "8088/tcp"]


@pytest.mark.asyncio
async def test_egress_mode_iptables_keeps_the_in_container_baseline(docker: dict[str, Any]) -> None:
    """The opt-out restores the old, advisory behaviour exactly, NET_ADMIN included."""
    await _start(docker, _config(egress_mode="iptables"), performer_published=True)
    calls = docker["calls"]()
    assert [a[0] for a in calls] == ["run", "port"]
    run = calls[0]
    assert run[run.index("--cap-add") + 1] == "NET_ADMIN"
    assert f"PERFORMER_EGRESS_ALLOWLIST={','.join(ALLOW)}" in run
    assert "--network" not in run
    assert "-p" in run


def test_egress_mode_defaults_to_the_proxy_and_is_validated() -> None:
    assert _config().egress_mode == "proxy"
    with pytest.raises(ValueError, match="egress_mode"):
        _config(egress_mode="nftables")
    with pytest.raises(ValueError, match="egress_mode"):
        PerformerEndpointConfig.model_validate({"id": "s", "roles": ["implementer"], "egress_mode": "iptables"})


def test_only_an_allowlist_in_proxy_mode_is_proxied() -> None:
    assert performer_egress.proxied(_config())
    assert not performer_egress.proxied(_config(egress_allowlist=None))
    assert not performer_egress.proxied(_config(egress_allowlist=[]))
    assert not performer_egress.proxied(_config(egress_mode="iptables"))


# -- stage-provider jobs without the sandbox --------------------------------------------------


def _launch(tmp_path: Path) -> StageJobLaunch:
    return StageJobLaunch(kind="qare", ref="card-1.qa.abc1234.1", provider="qare", job_dir=tmp_path / "job", token="t0k")


@pytest.mark.asyncio
async def test_a_stage_job_without_the_sandbox_runs_behind_the_proxy_too(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = DockerStageJobs(IMAGE, egress_allowlist=ALLOW, extra_hosts=["llm:192.0.2.10"])
    assert jobs.proxied
    name = await jobs.start(_launch(tmp_path))
    assert name == job_name(tmp_path / "job")
    calls = docker["calls"]()
    egress = name + EGRESS_SUFFIX
    assert not _can_reach(calls, name, "blocked.example")
    assert _can_reach(calls, name, "api.github.com")
    created = _index(calls, lambda a: a[:2] == ["network", "create"])
    proxy = _index(calls, _is_proxy_run)
    job = _index(calls, _is_performer_run)
    assert created < proxy < job
    run = calls[job]
    assert _option(run, "--network") == egress
    assert f"{EGRESS_PROXY_ENV}=http://{PROXY_IP}:3128" in run
    assert "--cap-add" not in run
    assert not any(a.startswith("--runtime") for a in run)
    assert not any(a.startswith("PERFORMER_EGRESS_ALLOWLIST") for a in run)
    proxy_args = calls[proxy]
    # No sandbox entry point keeps it alive, so it follows its job's (absolute) name instead,
    # pinned to the job's network.
    assert _option(proxy_args, "--follow") == f"{name}."
    assert _option(proxy_args, "--within") == "198.51.100.0/24"
    assert "--idle-exit" not in proxy_args
    assert not any(a.startswith("--runtime") for a in proxy_args)


@pytest.mark.asyncio
async def test_a_stage_job_whose_proxy_cannot_start_never_starts(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](proxy_fails="Error: no such image")
    jobs = DockerStageJobs(IMAGE, egress_allowlist=ALLOW)
    with pytest.raises(TransportError, match="start its proxy"):
        await jobs.start(_launch(tmp_path))
    assert not any(_is_performer_run(a) for a in docker["calls"]())


@pytest.mark.asyncio
async def test_a_stage_job_probe_with_an_allowlist_has_no_network(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = DockerStageJobs(IMAGE, egress_allowlist=ALLOW)
    await jobs.probe(["qare", "--version"])
    (probe,) = [a for a in docker["calls"]() if a[:1] == ["run"]]
    assert _option(probe, "--network") == "none"
    assert "--cap-add" not in probe


def test_a_stage_job_with_egress_mode_iptables_keeps_the_in_container_baseline() -> None:
    jobs = DockerStageJobs(IMAGE, egress_allowlist=["api.github.com"])
    jobs.egress_mode = "iptables"
    assert not jobs.proxied
    assert jobs._isolation()[:2] == ["--cap-add", "NET_ADMIN"]


def test_the_sandbox_always_uses_the_proxy_and_keeps_its_heartbeat_lifetime() -> None:
    jobs = DockerStageJobs(IMAGE, egress_allowlist=ALLOW, runtime="sysbox-runc")
    jobs.egress_mode = "iptables"
    assert jobs.proxied
    assert jobs._proxy_lifetime("job") == ["--idle-exit", "180"]


def test_a_stage_provider_takes_the_egress_mode_of_its_endpoint() -> None:
    provider = QAreProviderConfig(kind="qare", version="2026.11.44")
    for mode, proxied in (("iptables", False), ("proxy", True)):
        config = SimpleNamespace(agent_transport="docker", performer_endpoints=[_config(egress_mode=mode)])
        jobs = stage_jobs_for("qare", provider, config, role="qa")
        assert isinstance(jobs, DockerStageJobs)
        assert jobs.proxied is proxied, mode


# -- stopping a proxied performer removes its proxy and network at once ------------------------


@pytest.mark.asyncio
async def test_stopping_a_proxied_performer_removes_its_proxy_and_network(docker: dict[str, Any]) -> None:
    """Otherwise a fixed ``port`` stays held by the proxy and the next start cannot publish it,
    and every finished performer keeps a network out of Docker's address pools for a while."""
    docker["set"](label="coordinare-egress-aaaaaaaaaaaa")
    await DockerRuntime().stop("perf0", timeout_s=3)
    calls = docker["calls"]()
    stop = _index(calls, lambda a: a[:1] == ["stop"])
    assert calls[0][:2] == ["container", "inspect"]  # the label is read while the performer exists
    assert calls[stop][-1] == "perf0"
    assert "{{.State.Running}}" in calls[stop + 1]  # and it is confirmed gone before its way out goes
    assert calls[stop + 2:] == [["rm", "-f", "coordinare-egress-aaaaaaaaaaaa"], ["network", "rm", "coordinare-egress-aaaaaaaaaaaa"]]


@pytest.mark.asyncio
async def test_a_performer_that_did_not_stop_keeps_its_proxy_and_network(docker: dict[str, Any]) -> None:
    """``stop`` is best effort; a performer still running must keep its published port and exit."""
    docker["set"](label="coordinare-egress-aaaaaaaaaaaa", running="true")
    await DockerRuntime().stop("perf0")
    calls = docker["calls"]()
    assert not any(a[:2] in (["rm", "-f"], ["network", "rm"]) for a in calls)


@pytest.mark.asyncio
async def test_stopping_a_performer_without_a_proxy_only_stops_it(docker: dict[str, Any]) -> None:
    docker["set"](label="")
    await DockerRuntime().stop("perf0")
    assert [a[0] for a in docker["calls"]()] == ["container", "stop"]


@pytest.mark.asyncio
async def test_a_network_still_held_by_the_removed_performer_is_retried(docker: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """``docker stop`` returns before an ``--rm`` container is gone, so the first ``network rm`` can
    find it still attached."""
    monkeypatch.setattr(performer_egress, "_NETWORK_RM_RETRY_SECONDS", 0.0)
    docker["set"](network_rm_fails="error while removing network: has active endpoints")
    await performer_egress.down(_lifecycle_docker, "coordinare-egress-aaaaaaaaaaaa")
    rms = [a for a in docker["calls"]() if a[:2] == ["network", "rm"]]
    assert len(rms) == performer_egress.NETWORK_RM_ATTEMPTS
