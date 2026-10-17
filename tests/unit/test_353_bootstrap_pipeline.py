"""Exercise real contract publication, runner, clean verifier parsing and provenance."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import platform
import sys
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import (
    ExecutionCapabilities,
)
from coordinare_service_inference.environment.runner import run_manifest

from coordinare.config import EnvCacheConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService, verify_env_cache_clean
from coordinare.services.environment_acceptance import accepted_environment
from tests.unit.env_manifest_fixture import manifest_proposal, review_proposal, reviewed_cache

IMAGE = "trusted/pipeline@sha256:" + "a" * 64
SOURCE = "Install a persistent payload containing ready; verify its contents in a fresh consumer."
COMMIT = "a" * 40


def setup(tmp_path):
    root = tmp_path / "caches"
    root.mkdir()
    cache = EnvCacheState(symphony_name="suite", sanitised_name="suite", cache_dir=root / "suite",
        bootstrap_commit_sha=COMMIT, readme_sha="fingerprint")
    global_config = SimpleNamespace(performer_image=IMAGE, performer_endpoints=[], performer_log_dir=None,
        env_cache_root=root, state_file_path=tmp_path / "state" / "state.json", env_cache=EnvCacheConfig(),
        github_org="org", project_name="suite", env_bootstrap_max_attempts=3)
    config = SimpleNamespace(agent_transport="docker", global_config=global_config, symphonies=[])
    service = EnvCacheService(config)
    state = {"config": global_config, "coordinare_config": config, "env_cache": {"suite": cache}, "env_cache_service": service}
    capability = ExecutionCapabilities(schema_version=1, image_ref=IMAGE,
        platform=platform.system().lower() + "/" + platform.machine().lower(), cache_mount_path="/devenv/suite",
        install_network="enabled", verify_network="disabled")
    return service, cache, state, capability


class Repository:
    def __init__(self):
        self.head = COMMIT
        self.paths = ["guide"]

    async def get_branch_head_sha(self, branch):
        return self.head

    async def get_file_blob_sha(self, *args, **kwargs):
        return "unchanged-blob"

    async def get_repository_paths(self, *args, **kwargs):
        assert kwargs["ref"] == self.head
        return self.paths

    async def get_file_content(self, *args, **kwargs):
        assert kwargs["ref"] == self.head
        return SOURCE if args[2] == "guide" else None


class Proposal:
    def __init__(self, *, repair=False, verdict="accept"):
        self.repair = repair
        self.verdict = verdict
        self.invalid_sent = False
        self.review_starts = 0

    async def __call__(self, messages):
        context = json.loads(messages[1]["content"])
        reviewing = "manifest" in context
        if len(messages) == 2:
            self.review_starts += int(reviewing)
            return json.dumps({"tool": "read_files", "paths": ["guide"]})
        if reviewing:
            return json.dumps({"assessment": review_proposal({"schema_version": 1, "policy_version": "351-v1",
                "source_commit": context["source_commit"], "manifest_digest": context["manifest_digest"], "sources": ["guide"],
                "capabilities_digest": context["capabilities_digest"], "inventory_digest": context["inventory_digest"],
                "verdict": self.verdict, "rationale": "The required persistent payload has an independent executable check."})})
        data = {"schema_version": 1, "policy_version": "351-v1", "symphony_name": context["symphony_name"], "source_commit": context["source_commit"],
            "inputs": {"guide": hashlib.sha256(SOURCE.encode()).hexdigest()}, "inventory_digest": context["inventory_digest"],
            "capabilities_digest": context["capabilities_digest"], "requirements": [
                {"id": "payload", "description": SOURCE, "source": "guide", "checks": ["read-payload"]}],
            "install": [{"id": "write-payload", "argv": [sys.executable, "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ready')", "{cache}/payload"]}],
            "checks": [{"id": "read-payload", "argv": [sys.executable, "-c",
                "import pathlib,sys;assert pathlib.Path(sys.argv[1]).read_text()=='ready';assert pathlib.Path('guide').is_file()",
                "{cache}/payload"]}], "activation": {}, "services": [], "empty_reason": None}
        if self.repair and not self.invalid_sent:
            self.invalid_sent = True
            data["requirements"][0]["checks"] = ["not-an-executable-check-id"]
        return json.dumps({"manifest": manifest_proposal(data)})


async def publish(service, cache, state, capability, proposal):
    github = Repository()
    state["github_service"] = github
    await service._build_manifest_artifacts(performer_mounts=(cache.cache_dir.parent,), symphony_name="suite",
        repo="suite", github_org="org", github_service=github, readme_contents={}, current_sha="fingerprint",
        cache_dir=cache.cache_dir, cache_mount_path=capability.cache_mount_path, llm_chat=proposal,
        cache_state=cache, capabilities=capability)


async def local_clean_transport(monkeypatch, capability, cache):
    """Fake only container I/O; execute the actual trusted wrapper and shared runner."""
    original = asyncio.create_subprocess_exec

    class LocalProcess:
        def __init__(self, process):
            self.process = process

        @property
        def returncode(self):
            return self.process.returncode

        async def communicate(self, input=None):
            script = input.replace(b"/usr/local/bin/python", sys.executable.encode())
            script = script.replace(capability.cache_mount_path.encode(), str(cache.cache_dir).encode())
            return await self.process.communicate(input=script)

        def kill(self):
            self.process.kill()

        async def wait(self):
            return await self.process.wait()

    async def create(*args, **kwargs):
        if args[0] != "docker":
            return await original(*args, **kwargs)
        assert args[args.index("--network") + 1] == "none"
        assert args[args.index("-v") + 1].endswith(":ro")
        return LocalProcess(await original("bash", "-s", **kwargs))

    async def inspected(image):
        return capability.image_ref, capability.platform

    async def true(*args, **kwargs):
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity", inspected)
    monkeypatch.setattr("coordinare.services.env_cache._docker_daemon_reachable", true)
    monkeypatch.setattr("coordinare.services.env_cache._remove_verify_container", true)


@pytest.mark.asyncio
async def test_daemon_bootstrap_repair_clean_verify_freeze(tmp_path, monkeypatch):
    service, cache, state, capability = setup(tmp_path)
    proposal = Proposal(repair=True)
    await publish(service, cache, state, capability, proposal)
    assert proposal.invalid_sent and proposal.review_starts == 1
    manifest = accepted_environment(cache)
    assert manifest is not None
    repo, scratch = tmp_path / "installer", tmp_path / "scratch"
    repo.mkdir()
    scratch.mkdir()
    (repo / "guide").write_text(SOURCE)
    report = await run_manifest(manifest, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True)
    assert report.passed
    await local_clean_transport(monkeypatch, capability, cache)
    passed, reason = await verify_env_cache_clean(state, "suite", SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
    assert passed is True, reason
    assert cache.environment_verification["manifest_digest"] == manifest.digest()
    assert cache.environment_verification["image_ref"] == IMAGE
    frozen, reason = await service.freeze_provenance("suite", state)
    assert frozen, reason
    service.on_bootstrap_complete("suite", True, state)
    assert cache.cache_dir_ready and cache.last_bootstrap_succeeded is True
    assert service._manifest_path(cache).is_file()


@pytest.mark.asyncio
async def test_outer_retry_does_not_accept_partial_attempt(tmp_path):
    service, cache, state, capability = setup(tmp_path)
    reviewed_cache(cache, capabilities=capability)
    cache.rendered_activate_sh = "# accepted old activation"
    cache.cache_dir.mkdir()
    (cache.cache_dir / "activate.sh").write_text(cache.rendered_activate_sh)
    (cache.cache_dir / "payload").write_text("ready")
    # A caller's boolean cannot waive missing clean execution on a reviewed manifest.
    cache.environment_verification = None
    service.on_bootstrap_complete("suite", True, state)
    assert cache.last_bootstrap_succeeded is False
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_required_failure_holds_both_transports(tmp_path):
    from coordinare.services.environment_acceptance import saved_execution_accepted
    from tests.unit.test_265_provenance_k8s import freeze, logs, summary
    from tests.unit.test_265_provenance_k8s import setup as kubernetes_setup

    for status in ("fail", "indeterminate", "not_run", "missing"):
        subdir = tmp_path / status
        subdir.mkdir()
        service, cache, state, capability = setup(subdir)
        reviewed_cache(cache, capabilities=capability)
        cache.cache_dir.mkdir()
        cache.rendered_activate_sh = "# activation"
        (cache.cache_dir / "activate.sh").write_text(cache.rendered_activate_sh)
        (cache.cache_dir / "payload").write_text("ready")
        data = copy.deepcopy(cache.environment_verification)
        if status == "missing":
            data["results"] = []
        else:
            data["results"][0]["status"] = status
            data["results"][0]["exit_code"] = 1 if status == "fail" else None
        cache.environment_verification = data
        assert not saved_execution_accepted(cache)
        service.on_bootstrap_complete("suite", True, state)
        assert not cache.cache_dir_ready
        kdir = subdir / "kubernetes"
        kdir.mkdir()
        kservice, kcache, kstate, kdaemon, performer, _runtime = kubernetes_setup(kdir, [(True, logs(summary()))])
        await freeze(kservice, kstate, kdaemon, performer)
        passed, _ = await verify_env_cache_clean(kstate, "sym", performer, performer_mounts=())
        assert passed is True
        evidence = copy.deepcopy(kcache.environment_verification)
        if status == "missing":
            evidence["results"] = []
        else:
            evidence["results"][0]["status"] = status
            evidence["results"][0]["exit_code"] = 1 if status == "fail" else None
        kcache.environment_verification = evidence
        passed, _ = await verify_env_cache_clean(kstate, "sym", performer, performer_mounts=())
        assert passed is False


@pytest.mark.asyncio
async def test_capability_change_restarts_bootstrap_at_dispatch(tmp_path, monkeypatch):
    from coordinare.graph.nodes import dispatch_performer as node

    _service, cache, state, capability = setup(tmp_path)
    reviewed_cache(cache, capabilities=capability)
    cache.cache_dir_ready = True

    from unittest.mock import AsyncMock

    cache.cache_dir.mkdir()
    cache.rendered_activate_sh = "# trusted activation"
    cache.rendered_verify_sh = "# trusted verification"
    (cache.cache_dir / "activate.sh").write_text(cache.rendered_activate_sh)
    changed = AsyncMock(return_value=("trusted/changed@sha256:" + "b" * 64, capability.platform))
    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity", changed)
    monkeypatch.setattr("coordinare.services.env_cache._docker_daemon_reachable", AsyncMock(return_value=True))
    released = []
    terminal = await node._env_readiness_gate(state, SimpleNamespace(), "card", "implementer", "suite", cache,
        False, lambda: released.append(True))
    changed.assert_awaited_once_with(IMAGE)
    assert terminal is state
    assert released == [True]
    assert cache.runtime_health_failed
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_reviewed_report_without_frozen_provenance_holds(tmp_path):
    service, cache, state, capability = setup(tmp_path)
    reviewed_cache(cache, capabilities=capability)
    cache.cache_dir.mkdir()
    cache.rendered_activate_sh = "# activation"
    (cache.cache_dir / "activate.sh").write_text(cache.rendered_activate_sh)
    assert cache.provenance_digest is None
    service.on_bootstrap_complete("suite", True, state)
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_inventory_change_queues_a_new_bootstrap_with_no_old_approval(tmp_path, monkeypatch):
    from tests.unit.services.test_env_cache_phase4 import _make_cfg, _seed_state

    state, _cache_dir, cache = _seed_state(tmp_path, "suite")
    config, symphony = _make_cfg("suite", tmp_path)
    service = EnvCacheService(config)
    github = Repository()
    cache.readme_sha = await service._current_manifest_sha("suite", symphony, github,
        symphony.effective_config(config.global_config), cache)
    cache.last_bootstrap_succeeded = True
    previous = cache.readme_sha
    github.head = "b" * 40
    github.paths.append("nested/new-note")

    async def inspect(image):
        return image, "linux/x86_64"

    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity", inspect)
    dispatched = []

    async def dispatch(performer_id, payload):
        dispatched.append(payload)

    await service.check_and_trigger("suite", symphony, github, state, dispatch, llm_chat=Proposal())
    assert len(dispatched) == 1
    assert cache.bootstrap_in_flight
    assert not cache.cache_dir_ready
    assert cache.readme_sha != previous
    assert cache.environment_manifest["source_commit"] == "b" * 40
    assert cache.environment_verification is None


@pytest.mark.asyncio
async def test_declared_service_parse_error_does_not_log_source(tmp_path):
    from structlog.testing import capture_logs

    service, _cache, _state, _capability = setup(tmp_path)
    sensitive = 'repository-private-credential-value'

    class BrokenDeclarations:
        async def get_file_content(self, *args, **kwargs):
            return sensitive

    with capture_logs() as events:
        result = await service._fetch_declared_services(github_org="org", repo="suite", symphony_name="suite",
            github_service=BrokenDeclarations(), commit=COMMIT)
    assert result == []
    assert sensitive not in json.dumps(events)
