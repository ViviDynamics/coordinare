"""Resolve real runtime constraints before asking agents to propose an environment."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, NoReturn

from coordinare_service_inference.environment.guest_selection import guest_for
from coordinare_service_inference.environment.models import ExecutionCapabilities
from coordinare_service_inference.environment.runtime import RuntimeIdentity, RuntimePolicy
from coordinare_service_inference.environment.runtime_guest import foreign_platforms, guest_cpus
from coordinare_service_inference.environment.system_packages import (
    COMMAND as SYSTEM_PACKAGES_COMMAND,
)
from coordinare_service_inference.environment.system_packages import (
    ENV_FILE as SYSTEM_PACKAGES_ENV_FILE,
)

from coordinare.services.environment_acceptance import (
    accepted_environment,
    immutable_verifier_image,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare.models.env_cache import EnvCacheState


# Dettmore e2e-58: postgres ran in guest Docker with -p 127.0.0.1:5439:5432 and no runtime_endpoints forward, so
# docker exec health checks passed while every carrier-side database client got "connection refused".
GUEST_PORT_GUIDANCE = (
    "Guest Docker runs in a separate virtual machine, so a port a container publishes is open on the guest, never on "
    "the carrier: carrier commands reach it only through a runtime_endpoints forward. Publish the container port on "
    "every guest interface (docker run -p 5432:5432, never -p 127.0.0.1:5432:5432, which the forward cannot reach), "
    "declare {\"id\": \"postgres\", \"bind_address\": \"127.0.0.1\", \"guest_port\": 5432, \"outer_port\": 5432} in "
    "runtime_endpoints, and connect carrier clients to 127.0.0.1 at outer_port. docker exec runs inside the container, "
    "so a docker exec health check does not prove carrier clients can connect: check readiness from the carrier "
    "through the forwarded port as well."
)
# Dettmore e2e-61: no install step pulled pgvector/pgvector:pg17, so the service start had to pull it and its health
# check ran out first; clean verification has no network, so it could never pull it there.
GUEST_IMAGE_GUIDANCE = (
    "Pull every image a service or check runs in an install step (docker pull IMAGE, at a pinned tag or digest): clean "
    "verification has no network, and a service start that must pull its image can outrun its health check."
)


class EnvironmentCapabilityError(ValueError):
    """A trusted actionable configuration reason, never provider/source prose."""

    def __init__(self, message: str, *, code: str = "capability_unavailable") -> None:
        super().__init__(message)
        self.code = code


async def _docker_identity(image: str) -> tuple[str, str]:
    """Inspect local content identity without pulling or executing project code."""
    proc = await asyncio.create_subprocess_exec("docker", "image", "inspect", image,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        message = "environment runtime image is unavailable"
        raise EnvironmentCapabilityError(message)
    try:
        info = json.loads(stdout)[0]
        pinned = image if "@sha256:" in image else next(iter(info.get("RepoDigests") or []), None)
        if not isinstance(pinned, str):
            message = "environment runtime image has no immutable repository digest"
            raise EnvironmentCapabilityError(message)
        immutable_verifier_image(pinned)
        os_name, architecture = info["Os"], info["Architecture"]
        machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        message = "environment runtime image identity is malformed"
        raise EnvironmentCapabilityError(message) from exc
    else:
        return pinned, f"{os_name}/{machine}"


def platform_roles(capabilities: ExecutionCapabilities) -> dict[str, Any]:
    """Where each kind of work runs; installs and checks always run on the carrier's own CPU, never through emulation."""
    runtime = capabilities.runtime
    daemon = runtime.identity.guest_platform if runtime is not None else capabilities.platform
    return {"install_and_check_platform": capabilities.platform, "container_daemon_platform": daemon,
            "emulated_platforms": list(foreign_platforms(daemon)) if runtime is not None else []}


# Dettmore e2e-71: an init step used the database container before start created it, then a start of
# `docker run -d` was reported passed once spawned while health's TCP probe answered before the container existed;
# health's docker exec found no container, and teardown later found it. These are lifecycle facts, not recipes.
SERVICE_START_GUIDANCE = (
    "Each service's init runs before its start, so init cannot use anything the start creates. The start command "
    "runs as a supervised background process and passes as soon as it is spawned; health runs immediately after. "
    "So start should keep the service in the foreground for its whole life (for example docker run without -d, or "
    "exec of the server binary), not a launcher that returns at once. Health must itself wait for readiness: poll, "
    "within its own timeout, until the service answers through its own protocol (a client or readiness command "
    "against it, or docker exec into its container once that container is running), never only until a TCP port "
    "accepts, since a port can accept before the service exists or can serve. One-time setup that needs the running "
    "service, such as creating databases or extensions, belongs in health after readiness is confirmed.")


def carrier_process_policy(capabilities: ExecutionCapabilities) -> dict[str, Any] | None:
    """Describe the enforced private-runtime carrier boundary without inferring native transport policy."""
    if capabilities.runtime is None:
        return None
    resources = capabilities.runtime.resources
    return {"applies_to": "installer_and_check_carrier_processes", "linux_capabilities": [],
            "session_budget": {"seconds": resources.max_seconds, "container_cpus": resources.cpus,
                               "guest_cpus": guest_cpus(resources)},
            "no_new_privileges": True, "uid_zero_restores_capabilities": False,
            "guest_docker_ready_before_manifest_commands": True,
            "manifest_execution_order": ["guest_readiness", "activation_environment", "install_if_requested",
                "activation_path_validation", "service_init_start_health_if_installs_passed", "checks", "service_stop",
                "seal_if_install_passed", "runtime_teardown"],
            "runtime_ordering_guidance": "The coordinator opens the private guest and waits for its ready marker and "
                "successful Docker health before manifest commands. It supplies guest Docker activation to carrier "
                "commands. Manifest services run after the installer command phase, and only when every required "
                "install passed: otherwise each service step is not_run, naming the failed installs. Startup readiness does not guarantee "
                "continued daemon health; required checks and cleanup remain mandatory. Each service stop must remove every "
                "Docker container, volume (including an image's anonymous volume: docker rm -f -v) and custom network "
                "its service created; any left behind fails that stop. " + SERVICE_START_GUIDANCE + " " + GUEST_PORT_GUIDANCE + " " + GUEST_IMAGE_GUIDANCE + " To stop a process, write its pid to a file under "
                "{scratch} when it starts and kill that pid: pkill -f or pgrep -f inside a longer shell script "
                "matches the script's own shell, whose command line contains the pattern. A ${NAME} reference "
                "resolves only from activation variables, operator-supplied runtime variables, and local credentials "
                "(each owned by a service this manifest starts, usable only by service and check commands, never install "
                "or activation). Generate any other secret a check needs inside its command, for example "
                "NAME=$(openssl rand -hex 64) before the test command.",
            "installation_guidance": "Even UID 0 cannot assume permission for user, group or ownership changes. "
                "Use inspected tools and cache-local dependency artifacts under {cache}. System packages cannot be "
                "installed here: the carrier has no Linux capabilities, so apt-get or apt install, dpkg -i, apk add and "
                "dnf or yum install all fail, and plain apt-get update and download fail too (apt's fetch helper needs "
                "setgroups). To add native libraries or headers, run the daemon's own command: "
                + SYSTEM_PACKAGES_COMMAND + " (Debian package names, for example libssl-dev zlib1g-dev libyaml-dev). It "
                "fetches this carrier's own release packages without privileges, resolving dependencies against what "
                "the carrier already has, unpacks them under {cache}/deps, points each -dev package's lib<name>.so link "
                "at the carrier's runtime library when the carrier already has it, moves their pkg-config files under "
                "{cache}/deps, writes a launcher for each command-line program the packages contain in {cache}/deps/bin "
                "(put {cache}/deps/bin, never {cache}/deps/usr/bin, on activation's PATH to run them: unpacked "
                "programs cannot find their libraries or relocated scripts on their own), and writes {cache}/deps/"
                + SYSTEM_PACKAGES_ENV_FILE + ". Source that file in each "
                "command that builds against the packages (. {cache}/deps/" + SYSTEM_PACKAGES_ENV_FILE + " && "
                "./configure ...): it sets CPATH, LIBRARY_PATH, CPPFLAGS, LDFLAGS with an rpath, and PKG_CONFIG_PATH, so "
                "libraries load at run time without LD_LIBRARY_PATH. Call it again with more packages whenever needed. "
                "Prefer it to fetching .deb files by URL or calling apt-get yourself: the packages match the carrier's "
                "release, so they cannot clash with the libraries its own tools load, and no step is left out. Never "
                "put LD_LIBRARY_PATH in activation: activation applies to every later command, and a library there "
                "replaces the carrier's own copy for system tools such as curl. Guest service privileges are "
                "daemon-owned. "
                # Dettmore e2e-57: PostgreSQL's PGXS called clang-19 for LLVM bitcode, the repair replaced clang with a
                # stub script, and the extension's install then failed further on.
                "When a build calls an optional tool the carrier lacks, turn that feature off with the build's own "
                "option (for a PostgreSQL extension built with PGXS, make with_llvm=no) rather than installing a "
                "stand-in or faking the tool. "
                "Installation, every check and the service lifecycle share one session_budget; the guest emulates its "
                "CPU, so container image builds are slow. Prefer an official prebuilt release archive or an extracted "
                "distribution package over compiling a language runtime, database or other large tool from source; "
                "compile only what has no pinned prebuilt. "
                # Dettmore e2e-75: five installs copied a language runtime out of its official container image; each
                # failed differently (a newer C library, a missing interpreter, a broken move) and no repair left it.
                "Never copy a runtime, compiler or other tool out of a container image (docker cp, docker create "
                "and export, or a mounted image filesystem) into {cache}: its binaries load that image's own system "
                "libraries and expect its own paths, which the carrier lacks. A prebuilt archive must be built for "
                "the carrier's platform and release; otherwise install the version the repository pins with the "
                "language's own installer or version manager, or build that pinned source release, into {cache}. "
                # Dettmore e2e-49: install-ruby "passed" on ruby --version while openssl, psych and zlib were never
                # built; e2e-54: unpacked -dev packages left libssl.so pointing at a runtime library never unpacked.
                "An install's verification must load what it installs: a version flag proves only that a binary starts. "
                "For a language runtime, load every native module or library the repository uses (for example its TLS, "
                "compression, YAML or serialization and FFI modules) the way the repository loads it; for a library, "
                "link or load it. A runtime build can skip a module it could not configure and still finish, so treat a "
                "reported skipped or missing module as a failure. Point include, library, pkg-config and loader paths "
                "at the multiarch directories (usr/include/<triplet> and usr/lib/<triplet>, for example "
                "aarch64-linux-gnu), not only usr/lib. Example, building a runtime without privileges: fetch each "
                "native dependency's -dev package with the daemon's command above, then in the build command source "
                "{cache}/deps/" + SYSTEM_PACKAGES_ENV_FILE + " and configure with a {cache} prefix; then load "
                "each native module the repository needs and fail the install if any does not load."}


def _refuse_runtime(message: str) -> NoReturn:
    raise EnvironmentCapabilityError(message) from None


async def _remove_runtime_inspector(name: str) -> None:
    async def command(*args: str) -> tuple[int, bytes]:
        proc = await asyncio.create_subprocess_exec("docker", *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return proc.returncode or 0, stderr

    try:
        await command("rm", "--force", name)
        code, detail = await command("container", "inspect", name)
        if code == 0 or b"No such" not in detail:
            _refuse_runtime("private runtime inspection cleanup is unconfirmed")
    except (OSError, TimeoutError):
        _refuse_runtime("private runtime inspection cleanup is unconfirmed")


async def _inspect_runtime_bundle(image: str, guest_platform: str) -> dict[str, str]:
    """Hash the immutable carrier's platform bundle for one guest, with no project or network."""
    name = "coordinare-runtime-inspect-" + uuid.uuid4().hex
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--name", name, "--rm", "--network", "none", "--cap-drop", "ALL",
            "--label", "coordinare.runtime.inspection=true",
            "--security-opt", "no-new-privileges", "--memory", "128m", "--memory-swap", "128m",
            "--cpus", "1", "--pids-limit", "32", "--read-only", "--entrypoint", "python",
            image, "-m", "coordinare_service_inference.environment.runtime_bundle", guest_platform,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0 or len(stdout) > 16 * 1024:
            _refuse_runtime("private runtime bundle is unavailable")
        try:
            data = json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            _refuse_runtime("private runtime bundle identity is malformed")
        if not isinstance(data, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
            _refuse_runtime("private runtime bundle identity is malformed")
        return data
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await _remove_runtime_inspector(name)


async def _with_private_runtime(target: ExecutionCapabilities, runtime: Any, guest_platform: str) -> ExecutionCapabilities:
    try:
        policy = RuntimePolicy.model_validate(runtime).for_guest(guest_platform)
    except ValueError:
        _refuse_runtime("private runtime has no configured guest for the repository's platform requirement")
    try:
        actual = RuntimeIdentity.model_validate({
            **await _inspect_runtime_bundle(target.image_ref, guest_platform), "schema_version": 1,
            "carrier_image": target.image_ref, "carrier_platform": target.platform,
        })
    except ValueError:
        _refuse_runtime("private runtime bundle identity differs from configured policy")
    if actual != policy.identity:
        _refuse_runtime("private runtime bundle identity differs from configured policy")
    return ExecutionCapabilities.model_validate({**target.model_dump(), "schema_version": 2, "runtime": policy})


async def resolve_execution_capabilities(state: Mapping[str, Any], symphony_name: str) -> ExecutionCapabilities:
    """Fail closed when the current target cannot supply an immutable identity."""
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    cache = (state.get("env_cache") or {}).get(symphony_name)
    if global_config is None or cache is None:
        message = "environment runtime configuration is unavailable"
        raise EnvironmentCapabilityError(message)
    transport = getattr(config, "agent_transport", None) or getattr(global_config, "agent_transport", None)
    root = "/devenv"
    consumers = [endpoint for endpoint in getattr(global_config, "performer_endpoints", [])
                 if any(role != "env_bootstrap" for role in getattr(endpoint, "roles", []))]
    roots = {getattr(endpoint, "container_devenv_root", "/devenv") for endpoint in consumers}
    if len(roots) > 1:
        message = "environment consumers require one cache mount root"
        raise EnvironmentCapabilityError(message)
    if roots:
        root = roots.pop()
    runtime = getattr(getattr(global_config, "env_cache", None), "private_service_runtime", None)
    if runtime is not None and transport == "kubernetes":
        _refuse_runtime("private service runtime is not supported on Kubernetes")
    if transport == "kubernetes":
        image = getattr(global_config, "kubernetes_verifier_image", None) or getattr(global_config, "performer_image", None)
        platform = getattr(global_config, "kubernetes_verifier_platform", None)
        if not platform:
            message = "environment verifier platform must be configured"
            raise EnvironmentCapabilityError(message)
        if not getattr(getattr(global_config, "env_cache", None), "kubernetes_verifier_network_isolated", False):
            message = "environment verifier network isolation is required"
            raise EnvironmentCapabilityError(message)
    else:
        image = getattr(global_config, "performer_image", None)
        # Consumer endpoints, not a separate bootstrap-only image, define the cache target.
        images = {endpoint.image for endpoint in consumers if getattr(endpoint, "image", None)}
        if len(images) > 1:
            message = "environment consumers require one immutable target image"
            raise EnvironmentCapabilityError(message)
        if images:
            image = images.pop()
        if not image:
            message = "environment runtime image must be configured"
            raise EnvironmentCapabilityError(message)
        image, platform = await _docker_identity(image)
    target = ExecutionCapabilities(schema_version=1, image_ref=image, platform=platform,
        cache_mount_path=f"{root}/{cache.sanitised_name}", install_network="enabled", verify_network="disabled")
    if runtime is not None:
        # Derived from the pinned source's recorded requirements, so probes, approvals, execution and clean verification agree.
        required = (getattr(cache, "environment_guest", None) or {}).get("required") or []
        target = await _with_private_runtime(target, runtime, guest_for(target.platform, required))
    await _check_bootstrap_target(state, symphony_name, target)
    return target


async def _check_bootstrap_target(state: Mapping[str, Any], symphony_name: str, target: ExecutionCapabilities) -> None:
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    transport = getattr(config, "agent_transport", None) or getattr(global_config, "agent_transport", None)
    image, platform = target.image_ref, target.platform
    symphony = (state.get("symphony_configs") or {}).get(symphony_name)
    if symphony is None:
        symphony = next((item for item in getattr(config, "symphonies", []) if item.name == symphony_name), None)
    bootstrap_id = getattr(symphony, "env_bootstrap_performer_id", None)
    if bootstrap_id:
        bootstrap = next((item for item in getattr(global_config, "performer_endpoints", []) if item.id == bootstrap_id), None)
        bootstrap_image = getattr(bootstrap, "image", None) or getattr(global_config, "performer_image", None)
        if not isinstance(bootstrap_image, str) or not bootstrap_image:
            message = "bootstrap runtime image is unavailable"
            raise EnvironmentCapabilityError(message, code="bootstrap_identity")
        if transport == "kubernetes":
            if bootstrap_image != image:
                message = "bootstrap image differs from accepted target image"
                raise EnvironmentCapabilityError(message, code="bootstrap_identity")
        elif bootstrap_image != image:
            installed_image, installed_platform = await _docker_identity(bootstrap_image)
            if installed_image != image or installed_platform != platform:
                message = "bootstrap image or platform differs from accepted target"
                raise EnvironmentCapabilityError(message, code="bootstrap_identity")


def capabilities_match(cache: EnvCacheState, capabilities: ExecutionCapabilities) -> bool:
    """An independently accepted contract cannot be reused under different constraints."""
    manifest = accepted_environment(cache)
    return manifest is not None and manifest.capabilities_digest == capabilities.digest()


def installer_capabilities(cache: EnvCacheState) -> ExecutionCapabilities:
    target = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    if target.digest() != (cache.environment_manifest or {}).get("capabilities_digest"):
        message = "accepted environment runtime identity differs from installer target"
        raise ValueError(message)
    return target


def require_cache_mount(capabilities: ExecutionCapabilities, mount_path: str) -> None:
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError

    if capabilities.cache_mount_path != mount_path:
        message = "environment cache mount differs from runtime capabilities"
        raise EnvironmentDiscoveryError(message)
