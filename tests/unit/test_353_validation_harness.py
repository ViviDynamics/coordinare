"""The opt-in field harness cannot turn accepted proposals into unverified readiness."""

from __future__ import annotations

import json
import subprocess

import pytest
from scripts.validate_environment_repositories import (
    Profile,
    Snapshot,
    execution_authorized,
    final_readiness,
    new_profile_directory,
    validate_repository,
)


def test_scope_refuses_deployment_command_without_operator_review(tmp_path):
    assert not execution_authorized(
        tmp_path / "execution-approved.json",
        manifest_digest="a" * 64,
        source_commit="b" * 40,
        image="trusted@sha256:" + "c" * 64,
    )
    approval = {
        "scope": "deployment",
        "approved": True,
        "manifest_digest": "a" * 64,
        "source_commit": "b" * 40,
        "image_ref": "trusted@sha256:" + "c" * 64,
    }
    path = tmp_path / "execution-approved.json"
    path.write_text(json.dumps(approval))
    assert not execution_authorized(
        path, manifest_digest="a" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )
    approval["scope"] = "environment_validation"
    path.write_text(json.dumps(approval))
    assert execution_authorized(
        path, manifest_digest="a" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )
    assert not execution_authorized(
        path, manifest_digest="d" * 64, source_commit="b" * 40, image=approval["image_ref"],
    )


def test_probe_scope_is_separate_and_binds_exact_runtime(tmp_path):
    from scripts.validate_environment_repositories import probe_authorized

    from tests.unit.test_353_environment_probes import plan
    p = plan()
    path = tmp_path/'probe-approved.json'
    assert not probe_authorized(path, p)
    approval = {'approved':True, 'scope':'environment_probe', 'plan_digest':p.digest(),
        'source_commit':p.source_commit, 'image_ref':p.capabilities.image_ref, 'platform':p.capabilities.platform}
    path.write_text(json.dumps(approval))
    assert probe_authorized(path, p)
    assert not execution_authorized(path, manifest_digest=p.digest(), source_commit=p.source_commit,
        image=p.capabilities.image_ref)
    for key in ('scope', 'plan_digest', 'source_commit', 'image_ref', 'platform'):
        path.write_text(json.dumps({**approval,key:'wrong'}))
        assert not probe_authorized(path, p)


@pytest.mark.asyncio
async def test_probe_approval_timeout_cannot_execute_or_renew_deadline(tmp_path):
    import time

    from scripts.validate_environment_repositories import await_probe_approval

    from tests.unit.test_353_environment_probes import plan
    before = time.monotonic()
    with pytest.raises(TimeoutError):
        await await_probe_approval(tmp_path, plan(), seconds=0.01)
    assert time.monotonic() - before < 1
    assert (tmp_path/'probe-proposal.json').exists()
    assert not (tmp_path/'probe-approved.json').exists()


@pytest.mark.asyncio
async def test_repair_discovery_failure_reports_current_stage(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from coordinare.services.env_cache import EnvCacheService
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt
    from tests.unit.env_manifest_fixture import reviewed_cache

    calls = 0
    async def verify():
        return None
    snapshot = SimpleNamespace(name='suite', org='fixture', commit='a'*40, verify=verify)
    async def build(self, **kwargs):
        nonlocal calls
        calls += 1
        cache = kwargs['cache_state']
        if calls == 1:
            begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
            reviewed_cache(cache)
            return
        cache.environment_attempt['diagnostics'] = {'stage':'repair_preparation', 'error_code':'repair_evidence_unavailable'}
        raise EnvironmentDiscoveryError('mandatory evidence unavailable', code='repair_evidence_unavailable')
    async def execute(snapshot, service, state, **kwargs):
        state['env_cache']['suite'].environment_attempt['status'] = 'repair_pending'
        kwargs['result'].update(stage='installer', installer_ran=True)
    async def cleanup(*args):
        return True
    monkeypatch.setattr(EnvCacheService, '_build_manifest_artifacts', build)
    monkeypatch.setattr(EnvCacheService, 'prepare_bootstrap_generation', lambda *args:None)
    monkeypatch.setattr('scripts.validate_environment_repositories.execute_accepted', execute)
    monkeypatch.setattr('scripts.validate_environment_repositories._cleanup_case', cleanup)
    result = await validate_repository(snapshot, image='trusted@sha256:'+'a'*64,
        output=tmp_path/'profile', profile=Profile(model='fixture',base_url='http://localhost:1',execute=True),
        chat=lambda messages:None)
    assert calls == 2
    assert result['stage'] == 'repair_preparation'
    assert result['reason_code'] == 'repair_evidence_unavailable'
    assert result['ready_at_validation'] is False


@pytest.mark.parametrize(
    "values", [(True, None, True), (True, False, True), (False, True, True), (True, True, False)],
)
def test_missing_clean_evidence_never_ready(values):
    assert not final_readiness(installer_passed=values[0], clean_passed=values[1], frozen=values[2])


def test_profiles_and_snapshots_do_not_overwrite_baseline(tmp_path):
    root = tmp_path / "profile"
    new_profile_directory(root)
    (root / "report.json").write_text('{"status":"held"}')
    with pytest.raises(FileExistsError):
        new_profile_directory(root)
    assert (root / "report.json").read_text() == '{"status":"held"}'
    assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_hold_never_launches_installer(tmp_path, monkeypatch):
    source = tmp_path / "snapshot"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "guide").write_text("Use the documented tool")
    subprocess.run(["git", "-C", str(source), "add", "guide"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "snapshot",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    snapshot = Snapshot(source=source, commit=commit, org="fixture", name="sample")

    async def image_identity(image):
        return image, "linux/x86_64"

    async def invalid_model(messages):
        return "{}"

    async def forbidden(*args, **kwargs):
        raise AssertionError("a held proposal launched installation")

    monkeypatch.setattr(
        "coordinare.services.environment_capabilities._docker_identity", image_identity,
    )
    monkeypatch.setattr("scripts.validate_environment_repositories.execute_accepted", forbidden)
    result = await validate_repository(
        snapshot,
        image="trusted@sha256:" + "a" * 64,
        output=tmp_path / "attempt",
        profile=Profile(model="fixture", base_url="http://localhost:1", execute=True),
        chat=invalid_model,
    )
    assert result["status"] == "held"
    assert result["independent_review_reached"] is False
    assert result["installer_ran"] is False
    assert result["clean_consumer_ran"] is False
    assert result["live_daemon_exercised"] is False
    assert (source / "guide").read_text() == "Use the documented tool"


def test_image_must_contain_current_candidate_code(monkeypatch):
    from scripts.environment_validation_image import verify_image_code

    monkeypatch.setattr("scripts.environment_validation_image._docker", lambda *args: json.dumps({
        "platform": "linux/x86_64", "files": {"runner.py": "outdated"}}))
    with pytest.raises(ValueError, match="differs from the checkout"):
        verify_image_code("trusted@sha256:" + "c" * 64)


@pytest.mark.asyncio
async def test_scope_approval_cannot_waive_missing_clean_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.validate_environment_repositories import execute_accepted

    from coordinare.services.environment_acceptance import accepted_environment
    from tests.unit.env_manifest_fixture import reviewed_cache, verification_output
    from tests.unit.test_353_bootstrap_pipeline import setup

    service, cache, state, capabilities = setup(tmp_path)
    reviewed_cache(cache, capabilities=capabilities)
    target = tmp_path / "case"
    target.mkdir()
    snapshot = SimpleNamespace(name="suite", commit=cache.bootstrap_commit_sha)

    async def verify():
        return None

    snapshot.verify = verify
    (target / "execution-approved.json").write_text(json.dumps({"scope": "environment_validation", "approved": True,
        "manifest_digest": accepted_environment(cache).digest(), "source_commit": snapshot.commit,
        "image_ref": capabilities.image_ref}))

    async def installer(*args):
        report = json.loads(verification_output(cache.environment_manifest))
        report["platform"] = capabilities.platform
        return 0, (json.dumps(report) + "\n").encode()

    async def missing_clean(*args, **kwargs):
        return None, "external verifier unavailable"

    async def forbidden_freeze(*args):
        raise AssertionError("missing clean evidence reached freeze")

    monkeypatch.setattr("scripts.validate_environment_repositories._install", installer)
    monkeypatch.setattr("scripts.validate_environment_repositories.verify_env_cache_clean", missing_clean)
    monkeypatch.setattr(service, "freeze_provenance", forbidden_freeze)
    result = {"ready_at_validation": False}
    await execute_accepted(snapshot, service, state, image=capabilities.image_ref, target=target,
        profile=Profile(model="fixture", base_url="http://localhost:1"), result=result)
    assert result["installer_ran"] is True
    assert result["clean_consumer_ran"] is True
    assert result["ready_at_validation"] is False
    assert result["reason_code"] == "clean_consumer_held"


@pytest.mark.asyncio
async def test_private_installer_clone_ignores_host_git_filters(tmp_path, monkeypatch):
    import asyncio
    import shlex

    from scripts.validate_environment_repositories import _install

    from tests.unit.env_manifest_fixture import reviewed_cache
    from tests.unit.test_353_bootstrap_pipeline import setup

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".gitattributes").write_text("payload filter=host-filter\n")
    (source / "payload").write_text("source bytes\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "-qm", "snapshot"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    marker = tmp_path / "host-filter-ran"
    configuration = tmp_path / "host.gitconfig"
    configuration.write_text('[filter "host-filter"]\n\tsmudge = touch ' + shlex.quote(str(marker)) + '; cat\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configuration))
    real_exec = asyncio.create_subprocess_exec

    async def boundary(*args, **kwargs):
        if args[0] == "docker":
            raise RuntimeError("isolated execution boundary reached")
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary)
    _service, cache, _state, capabilities = setup(tmp_path)
    reviewed_cache(cache, capabilities=capabilities)
    target = tmp_path / "case"
    target.mkdir()
    with pytest.raises(RuntimeError, match="execution boundary"):
        await _install(Snapshot(source, commit, "fixture", "suite"), cache, capabilities.image_ref, target, 5)
    assert not marker.exists(), "repository attributes invoked a host Git filter outside the container"
    assert (target / "installer-source/payload").read_text() == "source bytes\n"


@pytest.mark.asyncio
@pytest.mark.parametrize('code,error,confirmed', [
    (0, b'', True), (1, b'Error: No such container: fixture', True),
    (1, b'Cannot connect to the Docker daemon', False),
])
async def test_cleanup_requires_confirmed_container_absence(monkeypatch, code, error, confirmed):
    from unittest.mock import AsyncMock

    from scripts.validate_environment_repositories import _remove_container

    process = AsyncMock()
    process.returncode = code
    process.communicate.return_value = (b'', error)
    monkeypatch.setattr('asyncio.create_subprocess_exec', AsyncMock(return_value=process))
    assert await _remove_container('fixture') is confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed,expected', [(True, 2), (False, 1)])
async def test_harness_archives_rejected_attempt_and_requires_new_scope(tmp_path, monkeypatch, confirmed, expected):
    from tests.unit.test_353_bootstrap_pipeline import probe_transport
    async def approve_probe(target, plan, **kwargs):
        from scripts.validate_environment_repositories import probe_authorized
        (target/'probe-approved.json').write_text(json.dumps({'approved':True,'scope':'environment_probe',
            'plan_digest':plan.digest(),'source_commit':plan.source_commit,'image_ref':plan.capabilities.image_ref,
            'platform':plan.capabilities.platform}))
        assert probe_authorized(target/'probe-approved.json', plan)
    monkeypatch.setattr('coordinare.services.environment_probe_service.execute_environment_probe', probe_transport)
    monkeypatch.setattr('scripts.validate_environment_repositories.await_probe_approval', approve_probe)

    from tests.unit.env_manifest_fixture import verification_output
    from tests.unit.test_353_bootstrap_pipeline import SOURCE, Proposal

    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source/'guide').write_text(SOURCE)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'source'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    image = 'trusted@sha256:'+'a'*64
    async def identity(_image):
        return image, 'linux/x86_64'
    monkeypatch.setattr('coordinare.services.environment_capabilities._docker_identity', identity)
    paths = []
    async def cleanup(target, cache, image):
        import shutil
        if cache.cache_dir.exists():
            shutil.rmtree(cache.cache_dir)
        return True
    monkeypatch.setattr('scripts.validate_environment_repositories._cleanup_case', cleanup)
    async def execute(snapshot, service, state, *, target, result, **kwargs):
        cache = state['env_cache'][snapshot.name]
        paths.append(cache.cache_dir)
        assert not (target/'execution-approved.json').exists()
        if len(paths) == 1:
            report = json.loads(verification_output(cache.environment_manifest))
            report.update(platform='linux/x86_64', image_ref=image)
            report['results'][0].update(status='fail', exit_code=2)
            service.record_bootstrap_execution(snapshot.name, state, report, cleanup_confirmed=confirmed)
            service.on_bootstrap_complete(snapshot.name, False, state)
        result.update(status='held', reason_code='execution_scope_not_approved')
    monkeypatch.setattr('scripts.validate_environment_repositories.execute_accepted', execute)
    output = tmp_path/'profile'
    agent = Proposal()
    result = await validate_repository(Snapshot(source, commit, 'fixture', 'suite'), image=image,
        output=output, profile=Profile(model='fixture', base_url='http://localhost:1', execute=True), chat=agent)
    assert len(paths) == expected, result
    assert agent.review_starts == expected
    assert len(result['attempts']) == expected
    assert (output/'attempt-000/accepted-manifest.json').exists()
    assert (output/'attempt-000/result.json').exists()
    if confirmed:
        assert paths[0] != paths[1]
        assert (output/'attempt-001/accepted-assessment.json').exists()
    assert not result['ready_at_validation']


def test_candidate_cleanup_releases_owned_registry_storage(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import environment_validation_image as images

    storage = {'owned_registry_volume', 'unrelated_volume'}
    registry = None
    def docker(*args, **kwargs):
        nonlocal registry
        if args[0] == 'run':
            registry = args[args.index('--name')+1]
        if args[0] == 'push':
            return 'digest: sha256:'+'a'*64
        if args[:2] == ('image', 'inspect'):
            return '[]'
        return ''
    def process(args, **kwargs):
        if args[1] == 'logs':
            return SimpleNamespace(returncode=0, stdout='listening on 127.0.0.1:5000', stderr='')
        if args[1] == 'rm':
            assert args[-1] == registry
            if '-v' in args or '--volumes' in args:
                storage.remove('owned_registry_volume')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(images, '_docker', docker)
    monkeypatch.setattr(images.subprocess, 'run', process)
    monkeypatch.setattr(images, 'verify_image_code', lambda image: {'image_ref':image})
    with images.candidate_image('base@sha256:'+'b'*64, tmp_path/'image'):
        assert 'owned_registry_volume' in storage
    assert storage == {'unrelated_volume'}, 'registry removal leaked its anonymous upload storage'


@pytest.mark.asyncio
@pytest.mark.parametrize('removal_confirmed', [True,False])
async def test_owned_cleanup_handles_readonly_directories_without_following_links(tmp_path,monkeypatch,removal_confirmed):
    import asyncio
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from scripts import validate_environment_repositories as harness
    monkeypatch.chdir(tmp_path)
    cache=SimpleNamespace(cache_dir=Path('case/cache'))
    target=Path('case/attempt')
    outside=tmp_path/'outside'
    outside.mkdir()
    (outside/'preserved').write_text('untouched')
    outside.chmod(0o555)
    roots=[cache.cache_dir,target/'installer-source']
    for root in roots:
        nested=root/'readonly'/'nested'
        nested.mkdir(parents=True)
        (nested/'artifact').write_text('owned')
        (root/'outside-link').symlink_to(outside,target_is_directory=True)
        nested.chmod(0o555)
        nested.parent.chmod(0o555)
    real_create=asyncio.create_subprocess_exec
    removed=[]
    async def create(*args,**kwargs):
        mount=args[args.index('-v')+1]
        owned=Path(mount.rsplit(':/cleanup',1)[0]).resolve()
        assert owned in [root.resolve() for root in roots]
        program=args[-1].replace("'/cleanup'",repr(str(owned)))
        return await real_create(sys.executable,'-c',program,**kwargs)
    async def remove(name):
        removed.append(name)
        return removal_confirmed
    monkeypatch.setattr(harness.asyncio,'create_subprocess_exec',create)
    monkeypatch.setattr(harness,'_remove_container',remove)
    assert await harness._cleanup_case(target,cache,'fixture-image') is removal_confirmed
    assert (outside/'preserved').read_text()=='untouched'
    assert outside.stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)
    if removal_confirmed:
        assert all(not root.exists() for root in roots)
        assert len(removed)==2
    else:
        assert len(removed)==1
