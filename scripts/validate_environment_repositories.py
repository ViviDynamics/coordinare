"""Opt-in private field validation; run with python -m scripts.validate_environment_repositories.

Inputs are existing pinned snapshots, not a deployment configuration. Accepted
proposals wait for a local operator scope marker before project commands run.
No live daemon or outer automatic retry loop is implied by these direct calls.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from coordinare_service_inference.environment.execution_source import read_source_reference
from scripts import environment_validation_cleanup
from scripts.environment_validation_image import candidate_image, verify_image_code

from coordinare.config import EnvCacheConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.conducting import OpenAiApiBackend
from coordinare.services.env_cache import (
    EnvCacheService,
    sanitise_symphony_name,
    verify_env_cache_clean,
)
from coordinare.services.environment_acceptance import (
    accepted_environment,
    execution_report,
    saved_execution_accepted,
)
from coordinare.services.environment_attempt import DiscoveryChatReply, EnvironmentDiscoveryError
from coordinare.services.environment_repair import journal_for, remaining_seconds

if TYPE_CHECKING:
    from coordinare_service_inference.environment.execution_source import ExecutionSourceBundle

    from coordinare.services.environment_discovery import ChatJson
    from coordinare.services.environment_probes import ProbePlan


@dataclass(frozen=True)
class Profile:
    model: str
    base_url: str
    api_key_env: str = "OPENAI_API_KEY"
    structured_transport: str = "json_schema"
    max_tokens: int = 32768
    effort: str | None = None
    phase_timeout: float = 120
    attempt_timeout: float = 600
    execute: bool = False
    approval_timeout: float = 600
    installer_timeout: float = 900
    save_transcripts: bool = False
    keep_caches: bool = False
    max_schema_repairs: int = 2
    max_coverage_revisions: int = 2
    max_execution_repairs: int = 2
    max_review_context_bytes: int = 1024 * 1024


@dataclass
class Snapshot:
    source: Path
    commit: str
    org: str
    name: str
    modes: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    async def git(self, *arguments: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        """Bound output and reap cancelled reads without touching the snapshot index."""
        env = dict(
            os.environ,
            GIT_OPTIONAL_LOCKS="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.source),
            "-c",
            "core.fsmonitor=false",
            "--no-pager",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        output = bytearray()
        try:
            async with asyncio.timeout(30):
                if proc.stdout is None:
                    raise ValueError("snapshot Git output unavailable")
                while True:
                    chunk = await proc.stdout.read(min(65536, max_bytes + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        raise EnvironmentDiscoveryError(
                            "snapshot source byte limit exceeded", code="snapshot_byte_limit",
                        )
                await proc.wait()
            if proc.returncode:
                raise EnvironmentDiscoveryError(
                    "pinned snapshot object is unavailable", code="snapshot_unavailable",
                )
            return bytes(output)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def verify(self) -> None:
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.commit):
            raise ValueError("snapshot requires an immutable commit")
        if (await self.git("rev-parse", "HEAD")).decode().strip() != self.commit:
            raise ValueError("snapshot checkout differs from selected commit")
        if await self.git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("snapshot contains local modifications")

    async def prepare_execution_source(
        self, owner: str, repo: str, *, commit: str, destination: Path, deadline: float,
    ) -> ExecutionSourceBundle:
        from scripts.environment_validation_source import prepare_snapshot_source

        if owner != self.org or repo != self.name:
            raise ValueError('snapshot repository identity differs')
        return await prepare_snapshot_source(self, commit=commit, destination=destination, deadline=deadline)

    async def get_branch_head_sha(self, branch_name: str = 'main') -> str:
        return self.commit

    async def get_file_blob_sha(self, org: str, repo: str, path: str, *, ref: str | None = None) -> str | None:
        from coordinare_service_inference.environment.models import source_path

        source_path(path)
        if org != self.org or repo != self.name or (ref is not None and ref != self.commit):
            raise ValueError('snapshot object identity differs')
        raw = await self.git('--literal-pathspecs', 'ls-tree', '-z', self.commit, '--', path)
        records = [record for record in raw.split(b'\0') if record]
        if len(records) != 1:
            return None
        metadata, name = records[0].split(b'\t', 1)
        _mode, kind, oid = metadata.decode('ascii').split()
        return oid if name.decode() == path and kind == 'blob' else None

    async def get_repository_paths(
        self, owner: str, repo: str, *, ref: str, max_entries: int = 100_000,
    ) -> list[str]:
        if ref != self.commit:
            raise ValueError("snapshot identity differs")
        entries = (await self.git("ls-tree", "-rz", "--full-tree", ref)).split(b"\0")
        self.modes = {}
        for entry in entries:
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, _digest = metadata.decode("ascii").split()
            if kind == "blob":
                self.modes[path.decode("utf-8")] = mode
        if len(self.modes) > max_entries:
            raise EnvironmentDiscoveryError("snapshot inventory entry limit exceeded")
        return sorted(self.modes)

    async def get_file_content(self, org: str, repo: str, path: str, *, ref: str = "HEAD") -> str | None:
        from coordinare_service_inference.environment.models import source_path

        if ref == 'HEAD':
            ref = self.commit
        if ref != self.commit:
            raise ValueError("snapshot identity differs")
        source_path(path)

        async def fetch(current: str) -> str | None:
            if current not in self.modes:
                return None
            raw = await self.git("show", ref + ":" + current, max_bytes=256 * 1024)
            try:
                return raw.decode("utf-8")
            except UnicodeError:
                return None

        return cast("str | None", await read_source_reference(
            path, {name for name, mode in self.modes.items() if mode == "120000"}, fetch,
        ))


def new_profile_directory(path: Path) -> None:
    """Never overwrite baseline evidence or chmod an unrelated existing directory."""
    path.mkdir(mode=0o700, parents=True, exist_ok=False)


def execution_authorized(
    path: Path, *, manifest_digest: str, source_commit: str, image: str,
) -> bool:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return False
        approval = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(approval, dict)
        and approval.get("approved") is True
        and approval.get("scope") == "environment_validation"
        and approval.get("manifest_digest") == manifest_digest
        and approval.get("source_commit") == source_commit
        and approval.get("image_ref") == image
    )


def probe_authorized(path: Path, plan: ProbePlan) -> bool:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return False
        approval = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(approval, dict) and approval.get("approved") is True
        and approval.get("scope") == "environment_probe" and approval.get("plan_digest") == plan.digest()
        and approval.get("source_commit") == plan.source_commit and approval.get("image_ref") == plan.capabilities.image_ref
        and approval.get("platform") == plan.capabilities.platform)


async def await_probe_approval(target: Path, plan: ProbePlan, *, seconds: float) -> None:
    _write(target / "probe-proposal.json", plan.model_dump(mode="json"))
    async with asyncio.timeout(seconds):
        while not probe_authorized(target / "probe-approved.json", plan):
            await asyncio.sleep(0.25)


def final_readiness(*, installer_passed: bool, clean_passed: bool | None, frozen: bool) -> bool:
    return installer_passed is True and clean_passed is True and frozen is True


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _event(**fields: Any) -> None:
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


async def _bounded_wait(process: asyncio.subprocess.Process, timeout: float) -> int:
    try:
        return await asyncio.wait_for(process.wait(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _remove_container(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "rm", "-f", name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(proc.communicate(), 30)
        return proc.returncode == 0 or b"No such container" in error
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _install(
    snapshot: Snapshot, cache: EnvCacheState, image: str, target: Path, timeout: float,
) -> tuple[int, bytes]:
    """A private local clone carries no host credential configuration into the image."""
    workspace = target / "installer-source"
    git_environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_NO_REPLACE_OBJECTS="1")
    cloned = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "clone",
        "--quiet",
        "--no-hardlinks",
        "--no-checkout",
        str(snapshot.source),
        str(workspace),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=git_environment,
    )
    if await _bounded_wait(cloned, 60):
        raise RuntimeError("private installer snapshot clone failed")
    checkout = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        "-c",
        "core.hooksPath=" + os.devnull,
        "checkout",
        "--quiet",
        "--detach",
        snapshot.commit,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=git_environment,
    )
    if await _bounded_wait(checkout, 60):
        raise RuntimeError("private installer snapshot checkout failed")
    name = "coordinare-env-validation-" + uuid.uuid4().hex[:12]
    manifest = accepted_environment(cache)
    if manifest is None:
        raise ValueError("accepted environment disappeared before installation")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "512",
        "-e",
        "BASH_ENV=",
        "-e",
        "ENV=",
        "-e",
        "_DEVENV_SOURCED=1",
        "-e",
        "_DEVENV_SKIP_SERVICES=1",
        "-v",
        str(cache.cache_dir) + ":/devenv/" + cache.sanitised_name,
        "-v",
        str(workspace) + ":/repo",
        "--entrypoint",
        "/usr/local/bin/python",
        image,
        "-I",
        "-m",
        "coordinare_service_inference.environment.execution_source_install",
        "--cache",
        "/devenv/" + cache.sanitised_name,
        "--repo",
        "/repo",
        "--scratch",
        "/tmp",
        "--install",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps({"manifest":manifest.model_dump(mode="json"),
                "execution_source":cache.environment_execution_source}, ensure_ascii=False).encode()), timeout,
        )
        assert process.returncode is not None
        return process.returncode, output
    finally:
        try:
            removed = await _remove_container(name)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if not removed:
            raise RuntimeError("installer cleanup could not be confirmed")


async def execute_accepted(
    snapshot: Snapshot,
    service: EnvCacheService,
    state: dict[str, Any],
    *,
    image: str,
    target: Path,
    profile: Profile,
    result: dict[str, Any],
) -> None:
    cache = state["env_cache"][snapshot.name]
    manifest = accepted_environment(cache)
    if manifest is None:
        raise ValueError("independent acceptance is required before execution")
    for key in ('installer_evidence', 'clean_evidence', 'clean_consumer_detail'):
        result.pop(key, None)
    result.update(stage='execution_scope', installer_ran=False, installer_passed=False,
        clean_consumer_ran=False, clean_consumer_passed=None, ready_at_validation=False)
    marker = target / "execution-approved.json"
    remaining = remaining_seconds(cache) if cache.environment_attempt is not None else profile.approval_timeout
    deadline = time.monotonic() + min(profile.approval_timeout, remaining)
    while not execution_authorized(
        marker, manifest_digest=manifest.digest(), source_commit=snapshot.commit, image=image,
    ):
        if time.monotonic() >= deadline:
            result.update(
                status="held", stage="execution_scope", reason_code="execution_scope_not_approved",
            )
            return
        await asyncio.sleep(1)
    await snapshot.verify()
    result.pop("installer_evidence", None)
    result.pop("clean_evidence", None)
    result.pop("clean_consumer_detail", None)
    result.pop("clean_consumer_passed", None)
    result.update(stage="installer", installer_ran=True, installer_passed=False, clean_consumer_ran=False)
    code, output = await _install(snapshot, cache, image, target, profile.installer_timeout)
    (target / "installer-output.json").write_bytes(output)
    report = execution_report(output.decode(), cache, publish=False)
    result["installer_passed"] = (
        code == 0
        and report.passed
        and report.platform == cache.environment_capabilities["platform"]
    )
    evidence = {**report.model_dump(mode='json'), 'image_ref':image}
    result['installer_evidence'] = evidence
    result['execution_source'] = cache.environment_execution_source
    if not result["installer_passed"]:
        evidence = {**report.model_dump(mode="json"), "image_ref": image}
        result.update(status="held", reason_code="installer_failed", installer_evidence=evidence)
        service.record_bootstrap_execution(snapshot.name, state, evidence, cleanup_confirmed=True)
        service.on_bootstrap_complete(snapshot.name, False, state)
        return
    result.update(stage="clean_consumer", clean_consumer_ran=True)
    passed, detail = await verify_env_cache_clean(
        state,
        snapshot.name,
        SimpleNamespace(_config=SimpleNamespace(image=image)),
        performer_mounts=(target,),
    )
    result.update(
        clean_consumer_passed=passed,
        clean_consumer_detail=detail,
        clean_evidence=cache.environment_verification,
    )
    if passed is not True:
        result.update(status="held", reason_code="clean_consumer_held")
        service.record_bootstrap_execution(snapshot.name, state, cache.environment_verification,
            cleanup_confirmed=passed is False)
        service.on_bootstrap_complete(snapshot.name, False, state)
        return
    frozen, reason = await service.freeze_provenance(snapshot.name, state)
    result.update(provenance_frozen=frozen, provenance_detail=reason)
    service.on_bootstrap_complete(
        snapshot.name,
        final_readiness(
            installer_passed=result["installer_passed"], clean_passed=passed, frozen=frozen,
        ),
        state,
    )
    result["ready_at_validation"] = cache.cache_dir_ready and saved_execution_accepted(cache)
    result.update(status="ready" if result["ready_at_validation"] else "held", stage="readiness")


async def _cleanup_case(target: Path, cache: EnvCacheState, image: str) -> bool:
    """Remove only private case contents, including files created with container ownership."""
    roots = [cache.cache_dir, target / "installer-source"]
    existing = [root for root in roots if root.is_dir() and not root.is_symlink()]
    if not existing:
        return True
    program = inspect.getsource(environment_validation_cleanup) + "\nremove_contents(Path('/cleanup'))"
    for root in existing:
        name = "coordinare-env-validation-cleanup-" + uuid.uuid4().hex[:12]
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--name", name,
            "--network",
            "none",
            "-v",
            str(root) + ":/cleanup",
            "--entrypoint",
            "/usr/local/bin/python",
            image,
            "-I",
            "-c",
            program,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if await _bounded_wait(proc, 60):
                return False
        finally:
            removed = await _remove_container(name)
        if not removed:
            return False
        root.rmdir()
    return True


async def validate_repository(
    snapshot: Snapshot, *, image: str, output: Path, profile: Profile, chat: ChatJson | None = None,
) -> dict[str, Any]:
    new_profile_directory(output)
    started = time.monotonic()
    cache = EnvCacheState(
        symphony_name=snapshot.name,
        sanitised_name=sanitise_symphony_name(snapshot.name),
        cache_dir=output / "cache",
        bootstrap_commit_sha=snapshot.commit,
        readme_sha=snapshot.commit,
    )
    settings = EnvCacheConfig(
        manifest_phase_timeout_seconds=profile.phase_timeout,
        manifest_attempt_timeout_seconds=profile.attempt_timeout,
        manifest_max_schema_repairs=profile.max_schema_repairs,
        manifest_max_coverage_revisions=profile.max_coverage_revisions,
        manifest_max_execution_repairs=profile.max_execution_repairs,
        manifest_max_review_context_bytes=profile.max_review_context_bytes,
    )
    global_config = SimpleNamespace(
        performer_image=image,
        performer_endpoints=[],
        performer_log_dir=None,
        env_cache_root=output,
        state_file_path=output.parent / "daemon-state" / (snapshot.name + ".json"),
        env_cache=settings,
        bootstrap_max_seconds=3600,
        github_org=snapshot.org,
        project_name=snapshot.name,
        env_bootstrap_max_attempts=3,
    )
    config = SimpleNamespace(
        agent_transport="subprocess", global_config=global_config, symphonies=[],
    )
    service = EnvCacheService(config)
    state = {
        "coordinare_config": config,
        "config": global_config,
        "env_cache": {snapshot.name: cache},
        "github_service": snapshot,
        "env_cache_service": service,
    }
    calls = 0
    review_starts = 0
    result = {
        "repository": snapshot.name,
        "source_commit": snapshot.commit,
        "image_ref": image,
        "status": "held",
        "stage": "discovery",
        "independent_review_reached": False,
        "installer_ran": False,
        "clean_consumer_ran": False,
        "ready_at_validation": False,
        "live_daemon_exercised": False,
        "outer_retry_loop_exercised": False,
        "profile": asdict(profile),
    }
    if chat is None:
        key = os.environ.get(profile.api_key_env)
        if not key:
            raise ValueError("configured model credential is unavailable")
        backend = OpenAiApiBackend(
            api_key=key,
            model=profile.model,
            base_url=profile.base_url,
            max_tokens=profile.max_tokens,
            effort=profile.effort,
            structured_transport=profile.structured_transport,
        )
        chat = backend.chat_json_with_metadata

    selected_chat = chat
    async def recorded(messages: list[dict[str, str]], *, response_schema: dict[str, Any] | None = None) -> str | DiscoveryChatReply:
        nonlocal calls, review_starts
        calls += 1
        reviewing = "manifest" in json.loads(messages[1]["content"])
        review_starts += int(reviewing and len(messages) == 2)
        if selected_chat is None:
            raise ValueError("validation chat unavailable")
        from coordinare.services.environment_response_schema import invoke_chat

        response = await invoke_chat(selected_chat, messages, schema=response_schema or {})
        text = response.text if isinstance(response, DiscoveryChatReply) else response
        if profile.save_transcripts:
            _write(
                output / f"chat-{calls:03}.json",
                {
                    "messages": messages,
                    "response_schema": response_schema,
                    "wire_response": response.wire_text if isinstance(response, DiscoveryChatReply) else None,
                    "response": text,
                    "usage": dict(response.usage or {})
                    if isinstance(response, DiscoveryChatReply)
                    else None,
                    "finish_reason": response.finish_reason
                    if isinstance(response, DiscoveryChatReply)
                    else None,
                },
            )
        _event(
            repository=snapshot.name,
            stage="review" if reviewing else "discovery",
            call=calls,
            response_bytes=len(text.encode()),
        )
        return response

    async def persist_attempt() -> None:
        _write(output / "environment-attempt.json", cache.environment_attempt)
        for index, report in enumerate((cache.environment_attempt or {}).get("probe_reports", [])):
            _write(output / f"probe-{index:03}" / "probe-report.json", report)

    async def approve_probe(plan: ProbePlan) -> None:
        if not profile.execute:
            raise EnvironmentDiscoveryError("probe execution is disabled for this profile", code="probe_execution_disabled")
        index = (cache.environment_attempt or {}).get("probe_batches", 1) - 1
        target = output / f"probe-{index:03}"
        new_profile_directory(target)
        _event(repository=snapshot.name, stage="probe_scope", plan_digest=plan.digest())
        await await_probe_approval(target, plan, seconds=min(profile.approval_timeout, remaining_seconds(cache)))

    state["snapshot_save_fn"] = persist_attempt
    state["environment_probe_authorize"] = approve_probe
    service.bind_kubernetes_state(state)
    attempts = []
    attempt_target = output
    try:
        await snapshot.verify()
        for generation in range(3):
            service.prepare_bootstrap_generation(cache)
            attempt_target = output / f"attempt-{generation:03}"
            new_profile_directory(attempt_target)
            result["stage"] = "repair_discovery" if generation else "discovery"
            await service._build_manifest_artifacts(
                performer_mounts=(output,), symphony_name=snapshot.name, repo=snapshot.name,
                github_org=snapshot.org, github_service=snapshot, readme_contents={},
                current_sha=snapshot.commit, cache_dir=cache.cache_dir,
                cache_mount_path="/devenv/" + cache.sanitised_name,
                llm_chat=recorded, cache_state=cache,
            )
            for name, data in (("accepted-manifest.json", cache.environment_manifest),
                               ("accepted-assessment.json", cache.environment_assessment),
                               ("execution-capabilities.json", cache.environment_capabilities),
                               ("execution-source.json", cache.environment_execution_source)):
                _write(attempt_target / name, data)
                _write(output / name, data)
            result.update(status="accepted_not_executed", stage="coverage",
                manifest_digest=(cache.environment_assessment or {})["manifest_digest"], generation=generation,
                execution_source=cache.environment_execution_source)
            _write(attempt_target / "discovery-result.json", result)
            _write(output / "discovery-result.json", result)
            _event(repository=snapshot.name, stage="coverage", status="accepted",
                generation=generation, manifest_digest=result["manifest_digest"])
            if profile.execute:
                async with asyncio.timeout(remaining_seconds(cache)):
                    await execute_accepted(snapshot, service, state, image=image,
                        target=attempt_target, profile=profile, result=result)
            attempts.append(dict(result))
            _write(attempt_target / "result.json", result)
            await persist_attempt()
            if journal_for(cache).status != "repair_pending":
                break
            # All executor cleanup was confirmed before admitting the rejected report.
            # Remove private source/cache contents before preparing another generation.
            if not await _cleanup_case(attempt_target, cache, image):
                result.update(status="held", reason_code="cleanup_unconfirmed")
                break
    except EnvironmentDiscoveryError as exc:
        result.update(status="held", reason_code=exc.code, reason=str(exc))
    except Exception as exc:
        result.update(
            status="held", reason_code="validation_stage_failed", error_type=type(exc).__name__,
        )
    finally:
        if isinstance(cache.environment_attempt, dict):
            diagnostics = cache.environment_attempt.get("diagnostics", {})
            result["diagnostics"] = diagnostics
            if result["stage"] in {"discovery", "repair_discovery"} and diagnostics.get("stage"):
                result["stage"] = diagnostics["stage"]
        result.update(
            seconds=round(time.monotonic() - started, 2),
            model_calls=calls,
            independent_review_reached=review_starts > 0,
            independent_review_invocations=review_starts,
            attempts=attempts,
        )
        if not profile.keep_caches and (result["installer_ran"] or result["clean_consumer_ran"]):
            result["private_runtime_contents_removed"] = await _cleanup_case(attempt_target, cache, image)
        try:
            await snapshot.verify()
            result["snapshot_unchanged"] = True
        except Exception:
            result.update(snapshot_unchanged=False, status="held", ready_at_validation=False,
                          reason_code="snapshot_integrity_changed")
        _write(output / "result.json", result)
        _event(
            repository=snapshot.name,
            status=result["status"],
            stage=result["stage"],
            seconds=result["seconds"],
        )
    return result


async def _batch(args: argparse.Namespace, image: str, profile: Profile) -> list[dict[str, Any]]:
    results = []
    if len(set(args.repositories)) != len(args.repositories):
        raise ValueError("duplicate repository selections")
    for name in args.repositories:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
            raise ValueError("invalid repository name")
        entry = args.snapshots / name
        inventory = json.loads((entry / "inventory.json").read_text())
        snapshot = Snapshot(entry / "source", inventory["commit"], args.org, name)
        results.append(
            await validate_repository(
                snapshot, image=image, output=args.output / name, profile=profile,
            ),
        )
        _write(args.output / "results.json", results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repositories", nargs="+")
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--org", default="ViviDynamics")
    images = parser.add_mutually_exclusive_group(required=True)
    images.add_argument("--image")
    images.add_argument("--base-image")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--effort")
    parser.add_argument("--structured-transport", choices=["json_schema", "tool_call", "json_object"], default="json_schema")
    parser.add_argument("--phase-timeout", type=float, default=120)
    parser.add_argument("--attempt-timeout", type=float, default=600)
    parser.add_argument("--max-schema-repairs", type=int, choices=range(3), default=2)
    parser.add_argument("--max-coverage-revisions", type=int, choices=range(3), default=2)
    parser.add_argument("--max-execution-repairs", type=int, choices=range(3), default=2)
    parser.add_argument("--max-review-context-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-timeout", type=float, default=600)
    parser.add_argument("--save-transcripts", action="store_true")
    parser.add_argument("--keep-caches", action="store_true")
    args = parser.parse_args()
    new_profile_directory(args.output)
    profile = Profile(
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        max_tokens=args.max_tokens,
        effort=args.effort,
        structured_transport=args.structured_transport,
        phase_timeout=args.phase_timeout,
        attempt_timeout=args.attempt_timeout,
        execute=args.execute,
        approval_timeout=args.approval_timeout,
        save_transcripts=args.save_transcripts,
        keep_caches=args.keep_caches,
        max_schema_repairs=args.max_schema_repairs,
        max_coverage_revisions=args.max_coverage_revisions,
        max_execution_repairs=args.max_execution_repairs,
        max_review_context_bytes=args.max_review_context_bytes,
    )
    revision = subprocess.check_output([shutil.which("git") or "/usr/bin/git", "rev-parse", "HEAD"], text=True).strip()
    _write(
        args.output / "profile.json",
        {
            "coordinare_commit": revision,
            "profile": asdict(profile),
            "snapshots": str(args.snapshots),
            "repositories": args.repositories,
            "org": args.org,
        },
    )
    _event(stage="image_preparation", status="started")
    context = (
        candidate_image(args.base_image, args.output / "image")
        if args.base_image
        else contextlib.nullcontext(args.image)
    )
    with context as image:
        _write(args.output / "image-identity.json", verify_image_code(image))
        results = asyncio.run(_batch(args, image, profile))
    return 0 if results and all(result["ready_at_validation"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
