"""447: the performer egress allowlist, enforced inside the QAre Sysbox sandbox.

A sandboxed QAre provider whose runs take an endpoint with ``egress_allowlist`` runs each job
on a Docker network of its own created with ``--internal`` (no route out), with a proxy
container (``performer.egress_proxy``) on that network and on the default bridge as its only
exit. Containers the inner Docker starts are NATed through the job's one interface, so they
have the same single exit. Nothing here runs Docker: a fake ``docker`` records every argument
list, and the network is modelled from those lists at the rule level.
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
from performer.egress_proxy import allowed, parse_allowlist

from coordinare.services.stage_contract import JobState
from coordinare.services.stage_jobs import (
    EGRESS_CREATED_LABEL,
    EGRESS_LABEL,
    EGRESS_PROXY_CHECK,
    EGRESS_PROXY_ENV,
    EGRESS_PROXY_IDLE_EXIT_SECONDS,
    EGRESS_PROXY_MODULE,
    EGRESS_SUFFIX,
    EGRESS_SWEEP_GRACE_SECONDS,
    SANDBOX_RUNTIME,
    DockerStageJobs,
    RunState,
    job_name,
)
from coordinare.services.stage_providers import stage_jobs_for
from coordinare.transport.base import TransportError
from tests.unit.test_435_qare_sandbox import (
    IMAGE,
    VERSION,
    _assert_never_the_host_or_privileged,
    _Config,
    _config,
    _entry_stubs,
    _everything,
    _jobs,
    _log,
    _qare,
    _request,
    _run_entry,
    _under_sysbox,
)

ALLOW = ["api.github.com", "llm"]
PROXY_IP = "198.51.100.2"

_FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(path))
def save():
    json.dump(state, open(path, "w"))
def fail(key, code=1):
    if state.get(key):
        sys.stderr.write(state[key] + "\n")
        sys.exit(code)
args = [a for a in args if not a.startswith("--runtime=")]
egress = state.get("egress_name", "")
if args[:1] == ["info"]:
    print(json.dumps({name: {"path": name} for name in state.get("runtimes", ["runc"])}))
elif args[:2] == ["network", "create"]:
    fail("network_create_fails")
    print("net0")
elif args[:2] == ["network", "connect"]:
    fail("connect_fails")
elif args[:2] == ["network", "rm"]:
    fail("network_rm_fails")
elif args[:2] == ["network", "ls"]:
    print(state.get("network_ls", ""))
elif args[:2] == ["run", "-d"] and "--entrypoint" in args:
    fail("proxy_fails", 125)
    print("proxy0")
elif args[:2] == ["run", "-d"]:
    fail("job_fails", 125)
    state["job"] = state.get("job_after_start", "running")
    save()
    print("job0")
elif args[:2] == ["container", "inspect"] and "{{json .NetworkSettings.Networks}}" in args:
    name = args[-1]
    print(json.dumps({name: {"IPAddress": state.get("proxy_ip", "198.51.100.2")}, "bridge": {"IPAddress": "198.51.100.9"}}))
elif args[:2] == ["container", "inspect"]:
    status = state.get("others", {}).get(args[-1], state.get("job", "missing"))
    if status == "missing":
        sys.stderr.write("Error: No such container: x\n")
        sys.exit(1)
    print(status)
elif args[:2] == ["rm", "-f"]:
    if not args[-1].endswith("-egress"):
        state["job"] = "missing"
        save()
elif args[:1] == ["run"]:
    if "docker info" in args[-1]:
        if not state.get("engine", "29.10.1"):
            sys.stderr.write("ModuleNotFoundError: No module named 'performer.egress_proxy'\n")
            sys.exit(1)
        print(state.get("engine", "29.10.1"))
    else:
        print(state.get("version", ""))
'''


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake ``docker`` that knows networks, a proxy container and the job container."""
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

    def clear() -> None:
        log.unlink(missing_ok=True)

    set_state(runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, engine="29.10.1")
    return {"bin": str(script), "set": set_state, "calls": calls, "clear": clear}


def _proxied(docker_bin: str) -> DockerStageJobs:
    return _jobs(docker_bin, "sysbox", egress=ALLOW)


async def _submit(work_dir: Path, docker: dict[str, Any], **state: Any) -> tuple[Any, Path, list[list[str]]]:
    """Submit one QAre request through a sandboxed provider with an allowlist."""
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, **state)
    request = _request()
    ref = request.request_id(1)
    job = await _qare(work_dir, docker["bin"], "sysbox", egress=ALLOW).submit(request, ref)
    return job, work_dir / ref, docker["calls"]()


def _index(calls: list[list[str]], match: Any) -> int:
    return next(i for i, args in enumerate(calls) if match(args))


def _is_job_run(args: list[str]) -> bool:
    return args[:1] == ["run"] and "-d" in args and "--entrypoint" not in args


def _is_proxy_run(args: list[str]) -> bool:
    return args[:1] == ["run"] and "--entrypoint" in args


def _option(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


# -- the network, modelled from the argument lists -----------------------------------------


def _topology(calls: list[list[str]]) -> tuple[dict[str, bool], dict[str, set[str]], dict[str, str]]:
    """Networks (name: internal?), containers (name: networks) and each proxy's allowlist.

    Docker's own networks are not internal. A container started without ``--network`` is on
    the default bridge; ``--network none`` is on nothing.
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
            name = _option(args, "--name") or "?"
            network = _option(args, "--network") or "bridge"
            containers[name] = set() if network == "none" else {network}
            if _is_proxy_run(args):
                proxies[name] = _option(args, "--allow") or ""
    return networks, containers, proxies


def _can_reach(calls: list[list[str]], container: str, host: str) -> bool:
    """Whether ``container``, or a container its inner Docker starts, can open a connection to ``host``.

    Inner containers are NATed out of the job container's interfaces, so they have exactly
    its networks. A direct route exists only through a network that is not internal; on an
    internal one, the only peers are the containers on it, and a proxy passes only what its
    allowlist allows (the proxy's own decision function decides).
    """
    networks, containers, proxies = _topology(calls)
    attached = containers[container]
    if any(not networks.get(n, False) for n in attached):
        return True
    peers = {name for name, nets in containers.items() if name != container and nets & attached}
    return any(name in proxies and allowed(host, parse_allowlist(proxies[name])) for name in peers)


@pytest.mark.asyncio
async def test_an_inner_container_is_refused_a_host_outside_the_allowlist(tmp_path: Path, docker: dict[str, Any]) -> None:
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    name = job_name(job_dir)
    assert not _can_reach(calls, name, "blocked.example")
    assert not _can_reach(calls, name, "evil-api.github.com")
    # The proxy itself has a route out, on the default bridge: it is the one exit.
    networks, containers, _ = _topology(calls)
    proxy = name + EGRESS_SUFFIX
    assert containers[name] == {proxy}
    assert networks[proxy] is True
    assert containers[proxy] == {"bridge", proxy}


@pytest.mark.asyncio
async def test_an_allowed_host_is_reached_through_the_proxy(tmp_path: Path, docker: dict[str, Any]) -> None:
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    name = job_name(job_dir)
    assert _can_reach(calls, name, "api.github.com")
    assert _can_reach(calls, name, "llm")
    start = next(a for a in calls if _is_job_run(a))
    assert f"{EGRESS_PROXY_ENV}=http://{PROXY_IP}:3128" in start


def test_the_model_sees_a_job_without_the_internal_network_as_unrestricted() -> None:
    """The model is not vacuous: a job on the default bridge reaches anything."""
    calls = [["run", "-d", "--name", "job", IMAGE, "true"]]
    assert _can_reach(calls, "job", "blocked.example")


# -- order, and what the job container gets ------------------------------------------------


@pytest.mark.asyncio
async def test_the_network_and_proxy_are_up_before_the_job_container_starts(tmp_path: Path, docker: dict[str, Any]) -> None:
    """The job's network namespace is born on the internal network: dockerd and QAre never see a route out."""
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    egress = job_name(job_dir) + EGRESS_SUFFIX
    created = _index(calls, lambda a: a[:2] == ["network", "create"])
    proxy = _index(calls, _is_proxy_run)
    connected = _index(calls, lambda a: a[:2] == ["network", "connect"])
    address = _index(calls, lambda a: "{{json .NetworkSettings.Networks}}" in a)
    job = _index(calls, _is_job_run)
    assert created < proxy < connected < address < job
    assert calls[created][-1] == egress
    assert "--internal" in calls[created]
    # No host address on its bridge: the job cannot reach host services through a gateway.
    assert calls[created][calls[created].index("-o") + 1] == "com.docker.network.bridge.inhibit_ipv4=true"
    assert calls[connected][2:] == [egress, egress]
    assert sum(1 for a in calls if _is_job_run(a)) == 1


@pytest.mark.asyncio
async def test_the_job_gets_only_the_internal_network_and_no_firewall_of_its_own(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    start = next(a for a in calls if _is_job_run(a))
    assert start[1] == f"--runtime={SANDBOX_RUNTIME}"
    assert start.count("--network") == 1
    assert _option(start, "--network") == job_name(job_dir) + EGRESS_SUFFIX
    assert "--cap-add" not in start
    assert not any(a.startswith("PERFORMER_EGRESS_ALLOWLIST") for a in start)
    assert "llm:192.0.2.10" in start  # the endpoint's extra_hosts still apply


@pytest.mark.asyncio
async def test_the_proxy_runs_unprivileged_with_the_allowlist(tmp_path: Path, docker: dict[str, Any]) -> None:
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    proxy = next(a for a in calls if _is_proxy_run(a))
    assert _option(proxy, "--name") == job_name(job_dir) + EGRESS_SUFFIX
    assert _option(proxy, "--user") == "65534:65534"
    assert "--read-only" in proxy
    assert "--rm" in proxy
    assert _option(proxy, "--entrypoint") == "python"
    assert proxy[proxy.index("--entrypoint") + 2:proxy.index("--entrypoint") + 5] == [IMAGE, "-m", EGRESS_PROXY_MODULE]
    assert _option(proxy, "--allow") == ",".join(ALLOW)
    assert _option(proxy, "--idle-exit") == str(EGRESS_PROXY_IDLE_EXIT_SECONDS)
    assert "llm:192.0.2.10" in proxy  # it resolves the endpoint's extra hosts as the job did
    assert "--network" not in proxy  # the default bridge: its route out
    assert f"{EGRESS_LABEL}={job_name(job_dir)}" in proxy
    assert "-v" not in proxy
    assert "--cap-add" not in proxy


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", ["sysbox", None])
async def test_nothing_a_proxied_provider_runs_gets_the_host_docker_or_privilege(
    tmp_path: Path, docker: dict[str, Any], sandbox: str | None,
) -> None:
    """#446's guard over every docker argument list, for the combination #446 refused, and (449)
    for an allowlist without the sandbox, which now runs behind the proxy too."""
    work_dir = tmp_path / "qare"
    calls = await _everything(_qare(work_dir, docker["bin"], sandbox, egress=ALLOW), work_dir, docker)
    job_dirs = {str(p) for p in work_dir.iterdir() if p.is_dir()}
    assert any(_is_proxy_run(a) for a in calls)
    assert any(_is_job_run(a) for a in calls)
    for args in calls:
        _assert_never_the_host_or_privileged(args, job_dirs)
        assert "--cap-add" not in args, args
        if args[:1] == ["run"] and sandbox:
            assert args[1] == f"--runtime={SANDBOX_RUNTIME}", args
        if args[:1] == ["run"] and not sandbox:
            assert not any(a.startswith("--runtime") for a in args), args


@pytest.mark.asyncio
async def test_probes_run_with_no_network(tmp_path: Path, docker: dict[str, Any]) -> None:
    ok, detail = await _qare(tmp_path / "qare", docker["bin"], "sysbox", egress=ALLOW).health()
    assert ok, detail
    runs = [a for a in docker["calls"]() if a[:1] == ["run"]]
    assert len(runs) == 2  # the engine check and the version check
    for args in runs:
        assert _option(args, "--network") == "none", args
        assert "--cap-add" not in args
        assert "--add-host" not in args
    assert not any(a[:1] == ["network"] for a in docker["calls"]())
    # The engine check also asks the image for the proxy module it will run.
    engine = next(a for a in runs if "docker info" in a[-1])
    assert engine[-1].endswith(f" && {EGRESS_PROXY_CHECK}")


@pytest.mark.asyncio
async def test_an_image_without_the_proxy_module_is_unhealthy_and_says_so(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, engine="")
    ok, detail = await _qare(tmp_path / "qare", docker["bin"], "sysbox", egress=ALLOW).health()
    assert not ok
    assert "performer.egress_proxy" in detail


# -- a setup that fails starts no job ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"network_create_fails": "Error response from daemon: could not find an available, non-overlapping IPv4 address pool"},
    {"proxy_fails": "docker: Error response from daemon: pull access denied"},
    {"connect_fails": "Error response from daemon: endpoint already exists"},
    {"proxy_ip": ""},
])
async def test_a_failed_egress_setup_never_starts_the_job(
    tmp_path: Path, docker: dict[str, Any], failure: dict[str, str],
) -> None:
    work_dir = tmp_path / "qare"
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, **failure)
    request = _request()
    ref = request.request_id(1)
    with pytest.raises(TransportError, match="egress proxy"):
        await _qare(work_dir, docker["bin"], "sysbox", egress=ALLOW).submit(request, ref)
    calls = docker["calls"]()
    assert not any(_is_job_run(a) for a in calls), "the job must not run without its proxy"
    egress = job_name(work_dir / ref) + EGRESS_SUFFIX
    # What was made is taken down again at once, the proxy before its network.
    failed = max(i for i, a in enumerate(calls) if a[:2] in (["network", "create"], ["network", "connect"])
                 or _is_proxy_run(a) or "{{json .NetworkSettings.Networks}}" in a)
    assert calls[failed + 1:failed + 3] == [["rm", "-f", egress], ["network", "rm", egress]], calls
    assert not (work_dir / ref).exists(), "the next dispatch starts afresh"


@pytest.mark.asyncio
async def test_a_job_that_fails_to_start_takes_its_proxy_down(tmp_path: Path, docker: dict[str, Any]) -> None:
    work_dir = tmp_path / "qare"
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, job_fails="docker: Error response from daemon: oops")
    request = _request()
    ref = request.request_id(1)
    with pytest.raises(TransportError, match="could not start"):
        await _qare(work_dir, docker["bin"], "sysbox", egress=ALLOW).submit(request, ref)
    egress = job_name(work_dir / ref) + EGRESS_SUFFIX
    calls = docker["calls"]()
    assert ["network", "rm", egress] in calls[_index(calls, _is_job_run):]


# -- the job already runs ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_that_already_runs_keeps_its_proxy(tmp_path: Path, docker: dict[str, Any]) -> None:
    """Started before a restart: starting it again must not cut it off from its proxy."""
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], job="running")
    jobs = _proxied(docker["bin"])
    from coordinare.services.stage_jobs import StageJobLaunch

    launch = StageJobLaunch(kind="qare", ref="r1", provider="qare", job_dir=tmp_path / "job")
    assert await jobs.start(launch) == job_name(tmp_path / "job")
    calls = docker["calls"]()
    assert not any(a[:1] in (["network"], ["rm"]) for a in calls), calls
    assert not any(a[:1] == ["run"] for a in calls)


# -- cleanup -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_removes_the_job_then_its_proxy_and_network(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _proxied(docker["bin"])
    await jobs.stop(tmp_path / "job")
    name = job_name(tmp_path / "job")
    assert docker["calls"]() == [["rm", "-f", name], ["rm", "-f", name + EGRESS_SUFFIX], ["network", "rm", name + EGRESS_SUFFIX]]


@pytest.mark.asyncio
async def test_a_job_seen_gone_takes_its_proxy_with_it_and_a_running_one_does_not(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    jobs = _proxied(docker["bin"])
    name = job_name(tmp_path / "job")
    docker["set"](job="running")
    assert await jobs.state(tmp_path / "job") is RunState.RUNNING
    assert not any(a[:1] in (["network"], ["rm"]) for a in docker["calls"]())
    docker["clear"]()
    docker["set"](job="exited")
    assert await jobs.state(tmp_path / "job") is RunState.GONE
    assert docker["calls"]()[1:] == [["rm", "-f", name + EGRESS_SUFFIX], ["network", "rm", name + EGRESS_SUFFIX]]


@pytest.mark.asyncio
async def test_an_overdue_job_after_a_restart_is_removed_with_its_proxy(tmp_path: Path, docker: dict[str, Any]) -> None:
    from tests.unit.test_435_qare_sandbox import _make_overdue

    work_dir = tmp_path / "qare"
    _job, job_dir, _calls = await _submit(work_dir, docker)
    docker["clear"]()
    _make_overdue(job_dir)
    job = await _qare(work_dir, docker["bin"], "sysbox", egress=ALLOW).poll(_job.ref)
    assert job.state is JobState.RETRYABLE
    name = job_name(job_dir)
    assert docker["calls"]() == [["rm", "-f", name], ["rm", "-f", name + EGRESS_SUFFIX], ["network", "rm", name + EGRESS_SUFFIX]]


def _ls(*rows: tuple[str, str, float | str]) -> str:
    return "\n".join(f"{job}{EGRESS_SUFFIX}\t{job}\t{created}" for job, _status, created in rows)


@pytest.mark.asyncio
async def test_a_start_sweeps_old_egress_networks_whose_job_is_gone(tmp_path: Path, docker: dict[str, Any]) -> None:
    old = time.time() - EGRESS_SWEEP_GRACE_SECONDS - 60
    rows = [
        ("coordinare-stage-gone", "missing", old),        # a job that ended long ago: swept
        ("coordinare-stage-exited", "exited", old),       # ended, not removed yet: swept
        ("coordinare-stage-runs", "running", old),        # still running: kept
        ("coordinare-stage-young", "missing", time.time()),  # being started right now: kept
        ("coordinare-stage-nodate", "missing", ""),       # no date: old enough
    ]
    others = {f"{job}": status for job, status, _ in rows}
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], network_ls=_ls(*rows), others=others)
    from coordinare.services.stage_jobs import StageJobLaunch

    await _proxied(docker["bin"]).start(StageJobLaunch(kind="qare", ref="r1", provider="qare", job_dir=tmp_path / "job"))
    removed = {a[-1] for a in docker["calls"]() if a[:2] == ["network", "rm"]}
    assert "coordinare-stage-gone-egress" in removed
    assert "coordinare-stage-exited-egress" in removed
    assert "coordinare-stage-nodate-egress" in removed
    assert "coordinare-stage-runs-egress" not in removed
    assert "coordinare-stage-young-egress" not in removed
    ls = next(a for a in docker["calls"]() if a[:2] == ["network", "ls"])
    assert f"label={EGRESS_LABEL}" in ls
    assert EGRESS_CREATED_LABEL in ls[-1]


@pytest.mark.asyncio
async def test_the_egress_network_carries_its_job_and_its_age(tmp_path: Path, docker: dict[str, Any]) -> None:
    _job, job_dir, calls = await _submit(tmp_path / "qare", docker)
    create = next(a for a in calls if a[:2] == ["network", "create"])
    assert f"{EGRESS_LABEL}={job_name(job_dir)}" in create
    created = next(a for a in create if a.startswith(f"{EGRESS_CREATED_LABEL}="))
    assert abs(float(created.split("=", 1)[1]) - time.time()) < 60


# -- config: the combination #446 refused is available -------------------------------------


def test_a_sandbox_with_an_allowlisted_endpoint_runs_behind_the_proxy() -> None:
    config = _Config()
    config.performer_endpoints = [SimpleNamespace(
        name="qa-pool", mode="docker", image=IMAGE, roles=["qa"], egress_allowlist=ALLOW, extra_hosts=[],
    )]
    jobs = stage_jobs_for("qare", _config("sysbox"), config, role="qa")
    assert isinstance(jobs, DockerStageJobs)
    assert jobs.proxied
    assert jobs.runtime == SANDBOX_RUNTIME
    off = stage_jobs_for("qare", _config(None), config, role="qa")
    assert isinstance(off, DockerStageJobs)
    # 449: without the sandbox the allowlist is enforced by the proxy too, unless the endpoint
    # asks for the old in-container rules.
    assert off.proxied
    assert off.runtime is None
    config.performer_endpoints[0].egress_mode = "iptables"
    iptables = stage_jobs_for("qare", _config(None), config, role="qa")
    assert isinstance(iptables, DockerStageJobs)
    assert not iptables.proxied


# -- off is unchanged ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_the_sandbox_the_allowlist_is_the_entry_points_as_before(tmp_path: Path, docker: dict[str, Any]) -> None:
    """449: with egress_mode: iptables. The default (the proxy) is in test_449_performer_egress.py."""
    work_dir = tmp_path / "qare"
    calls = await _everything(_qare(work_dir, docker["bin"], None, egress=ALLOW, egress_mode="iptables"), work_dir, docker)
    assert not any(a[:1] == ["network"] for a in calls)
    assert not any(_is_proxy_run(a) for a in calls)
    for args in (a for a in calls if a[:1] == ["run"]):
        assert "--network" not in args
        assert args[args.index("--cap-add") + 1] == "NET_ADMIN"
        assert f"PERFORMER_EGRESS_ALLOWLIST={','.join(ALLOW)}" in args


@pytest.mark.asyncio
async def test_a_sandbox_without_an_allowlist_makes_no_network(tmp_path: Path, docker: dict[str, Any]) -> None:
    work_dir = tmp_path / "qare"
    calls = await _everything(_qare(work_dir, docker["bin"], "sysbox"), work_dir, docker)
    assert not any(a[:1] == ["network"] for a in calls)
    assert not any("--network" in a or EGRESS_PROXY_ENV in " ".join(a) for a in calls)


# -- the sandbox entry point ---------------------------------------------------------------


def _with_proxy_probe(stubs: dict[str, Path], *, answers: bool) -> dict[str, str]:
    """A stand-in interpreter that logs the proxy check; and dockerd logging its proxy variables."""
    probe = stubs["bin"] / "probe-python"
    probe.write_text(f'#!/bin/sh\necho "probe $3 $4" >> "{stubs["log"]}"\nexit {0 if answers else 1}\n')
    probe.chmod(0o755)
    dockerd = stubs["bin"] / "dockerd"
    body = dockerd.read_text().replace('echo "dockerd $*"', 'echo "dockerd $* proxy=$HTTPS_PROXY no=$NO_PROXY"')
    dockerd.write_text(body)
    base = stubs["base"]
    base.write_text(f'#!/bin/sh\necho "base $* proxy=$HTTPS_PROXY no=$NO_PROXY" >> "{stubs["log"]}"\n')
    return {"SANDBOX_PYTHON": str(probe), "SANDBOX_PROXY_WAIT_SECONDS": "1", "SANDBOX_PROXY_HEARTBEAT_SECONDS": "5",
            EGRESS_PROXY_ENV: f"http://{PROXY_IP}:3128"}


def test_the_entry_point_waits_for_the_proxy_before_dockerd_and_routes_through_it(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path)
    _under_sysbox(stubs)
    result = _run_entry(stubs, _with_proxy_probe(stubs, answers=True), "true")
    assert result.returncode == 0, result.stderr
    log = _log(stubs)
    assert log[0] == f"probe {PROXY_IP} 3128", log
    rest = [line for line in log[1:] if not line.startswith("probe")]  # later heartbeats aside
    dockerd = next(line for line in rest if line.startswith("dockerd"))
    proxy = f"proxy=http://{PROXY_IP}:3128 no=localhost,127.0.0.1,::1"
    assert dockerd.endswith(proxy), "dockerd pulls images through the proxy"
    assert rest[-1] == f"base true {proxy}", "the job's own traffic goes through the proxy; localhost does not"


def test_the_entry_point_keeps_the_proxy_alive_while_the_job_runs_and_stops_after(tmp_path: Path) -> None:
    """The proxy exits when idle: heartbeats while the job runs, none once it has ended."""
    stubs = _entry_stubs(tmp_path)
    env = _with_proxy_probe(stubs, answers=True)
    env["SANDBOX_PROXY_HEARTBEAT_SECONDS"] = "0.2"
    stubs["base"].write_text(f'#!/bin/sh\necho "base $*" >> "{stubs["log"]}"\nsleep 1.5\necho "base done" >> "{stubs["log"]}"\n')
    result = _run_entry(stubs, env, "true")
    assert result.returncode == 0, result.stderr
    time.sleep(1)  # any heartbeat still running has had its chance
    log = _log(stubs)
    done = log.index("base done")
    heartbeats = [i for i, line in enumerate(log) if line.startswith("probe")][1:]  # the first is the wait
    assert len([i for i in heartbeats if i < done]) >= 3, log
    assert len([i for i in heartbeats if i > done]) <= 1, log  # at most one already under way


def test_a_proxy_that_does_not_answer_stops_the_container_before_dockerd(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path)
    _under_sysbox(stubs)
    result = _run_entry(stubs, _with_proxy_probe(stubs, answers=False), "true")
    assert result.returncode != 0
    assert "egress proxy" in result.stderr
    assert "refusing to start" in result.stderr
    log = _log(stubs)
    assert log and all(line.startswith("probe") for line in log), log


def test_without_a_proxy_the_entry_point_checks_none(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path)
    env = _with_proxy_probe(stubs, answers=False)
    del env[EGRESS_PROXY_ENV]
    result = _run_entry(stubs, env, "true")
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("probe") for line in _log(stubs))
    assert _log(stubs)[-1] == "base true proxy= no="
