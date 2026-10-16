from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinare.__main__ import _build_arg_parser
from coordinare._version import __version__
from coordinare.dashboard import assets
from coordinare.doctor import DoctorReport
from coordinare.metrics import _coordinare_version

ROOT = Path(__file__).resolve().parents[2]
VERSION = "2026.10.84"
LEGACY_TAG = "2026.09.01.3"
NORMALIZED_VERSION = "2026.9.1.3"
RELEASE_ID = 193
EXECUTABLE_MODE = 0o755


def release_tools():
    spec = importlib.util.spec_from_file_location("release_packages", ROOT / "scripts/release_packages.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/main-branch-build.yml").read_text())


def test_version_flag_exits_without_loading_config(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("coordinare.__main__._coordinare_version", return_value=VERSION),
        pytest.raises(SystemExit) as result,
    ):
        _build_arg_parser().parse_args(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"coordinare {VERSION}"


def test_doctor_and_dashboard_report_installed_version() -> None:
    with patch("coordinare.doctor._coordinare_version", return_value=VERSION):
        assert VERSION in DoctorReport().render()
    with patch("coordinare.dashboard.assets._coordinare_version", return_value=VERSION):
        assert f"coordinare {VERSION}" in assets._page_html(False)
        assert f"coordinare {VERSION}" in assets._page_html(True)


def test_release_is_draft_until_its_assets_succeed() -> None:
    jobs = workflow()["jobs"]
    create = jobs["create-tag"]["steps"][-1]["with"]["script"]
    assert "draft: true" in create
    assert "make_latest: 'false'" in create
    # Draft releases do not create a ref. Image and package checkouts need a real tag.
    assert "github.rest.git.createRef" in create
    assert "refs/tags/" in create
    assert "published" in jobs["create-tag"]["outputs"]
    assert jobs["create-tag"]["outputs"]["release_id"] == "${{ steps.release.outputs.release_id }}"
    publish = jobs["publish-release"]
    assert set(publish["needs"]) == {"create-tag", "publish-artifacts"}
    assert "published != 'true'" in publish["if"]
    assert jobs["tag-latest"]["needs"] == [
        "create-tag", "manifest-base", "manifest-full", "build-daemon",
    ]
    script = publish["steps"][-1]["with"]["script"]
    assert "draft: false" in script
    assert "make_latest: 'true'" in script
    assert "always()" not in str(publish)
    assert publish["steps"][-1]["env"]["RELEASE_ID"] == "${{ needs.create-tag.outputs.release_id }}"


def test_release_build_uses_tag_and_attaches_checksums_and_provenance() -> None:
    job = workflow()["jobs"]["release-artifacts"]
    assert "published != 'true'" in job["if"]
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ needs.create-tag.outputs.version }}"
    assert checkout["with"]["persist-credentials"] is False
    assert job["permissions"] == {"contents": "read"}
    build = next(s for s in steps if s.get("id") == "build")
    assert build["env"]["RELEASE_VERSION"] == "${{ needs.create-tag.outputs.version }}"
    assert "release_packages.py" in build["run"]
    assert "smoke_release.py" in str(steps)
    assert "uv pip sync" in str(steps)
    smoke_step = next(s for s in steps if "smoke_release.py" in s.get("run", ""))
    assert "--require-hashes" in smoke_step["run"]
    assert "--no-deps" in smoke_step["run"]
    assert any("performer-*.whl" in s.get("run", "") for s in steps)
    export = next(s for s in steps if s.get("id") == "export")
    assert "uv export --frozen --no-dev --no-emit-local" in export["run"]
    assert "--no-hashes" not in export["run"]
    assert "agent/performer" in export["run"]
    transfer = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert transfer["with"]["path"] == "dist/"
    assert transfer["with"]["if-no-files-found"] == "error"
    assert transfer["with"]["overwrite"] is True
    upload_job = workflow()["jobs"]["publish-artifacts"]
    assert set(upload_job["needs"]) == {"create-tag", "release-artifacts", "installer-smoke"}
    assert upload_job["permissions"] == {
        "contents": "write", "id-token": "write", "attestations": "write",
    }
    steps = upload_job["steps"]
    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in steps)
    assert not any("uv " in s.get("run", "") for s in steps)
    download = next(s for s in steps if s.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["name"] == transfer["with"]["name"]
    assert download["with"]["path"] == "dist/"
    attestation = next(s for s in steps if s.get("id") == "attest")
    assert attestation["uses"].startswith("actions/attest@")
    assert attestation["with"]["subject-path"] == "dist/*"
    assert attestation["with"]["create-storage-record"] is False
    upload = next(s for s in steps if s.get("id") == "upload")
    assert "sha256sum" not in upload["run"]
    assert "gh release upload" in upload["run"]
    assert "dist/*" in upload["run"]
    assert steps.index(attestation) < steps.index(upload)
    assert "test_142_license_and_legal_posture.py" in str(workflow()["jobs"]["test"])


def test_release_checksum_order_and_strict_verification() -> None:
    jobs = workflow()["jobs"]
    build_steps = jobs["release-artifacts"]["steps"]
    checksum = next(s for s in build_steps if s.get("id") == "checksums")
    transfer = next(s for s in build_steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    for smoke in (s for s in build_steps if "smoke_release.py" in s.get("run", "") or "performer-*.whl" in s.get("run", "")):
        assert build_steps.index(smoke) < build_steps.index(checksum)
    assert build_steps.index(checksum) < build_steps.index(transfer)
    assert checksum["working-directory"] == "dist"
    steps = jobs["publish-artifacts"]["steps"]
    download = next(s for s in steps if s.get("uses", "").startswith("actions/download-artifact@"))
    verify = next(s for s in steps if s.get("id") == "verify")
    attest = next(s for s in steps if s.get("id") == "attest")
    cleanup = next(s for s in steps if s.get("id") == "cleanup")
    upload = next(s for s in steps if s.get("id") == "upload")
    assert steps.index(download) < steps.index(verify) < steps.index(attest) < steps.index(cleanup) < steps.index(upload)
    assert "set -euo pipefail" in verify["run"]
    assert "sha256sum --strict -c SHA256SUMS" in verify["run"]
    assert verify["working-directory"] == "dist"
    assert "continue-on-error" not in str(steps)


def run_artifact_step(step: dict, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=root / step.get("working-directory", "."),
        env={**os.environ, "RUNNER_TEMP": str(root), "RELEASE_VERSION": VERSION},
        capture_output=True, text=True, check=False,
    )


def prepare_release_dist(root: Path) -> bytes:
    dist = root / "dist"
    dist.mkdir()
    for name in ("coordinare.whl", "performer.whl", "coordinare.tar.gz", "install.sh"):
        (dist / name).write_text(name)
    steps = workflow()["jobs"]["release-artifacts"]["steps"]
    checksum = next(s for s in steps if s.get("id") == "checksums")
    result = run_artifact_step(checksum, root)
    assert result.returncode == 0, result.stderr
    manifest = (dist / "SHA256SUMS").read_bytes()
    for name in (p.name for p in dist.iterdir() if p.name != "SHA256SUMS"):
        digest = hashlib.sha256((dist / name).read_bytes()).hexdigest()
        assert f"{digest}  {name}\n".encode() in manifest
    return manifest


@pytest.mark.parametrize("damage", ["none", "changed", "missing", "missing_manifest", "extra", "hidden", "malformed", "duplicate", "symlink", "directory"])
def test_release_verification_rejects_any_unverified_download(tmp_path: Path, damage: str) -> None:
    manifest = prepare_release_dist(tmp_path)
    dist = tmp_path / "dist"
    if damage == "changed":
        (dist / "coordinare.whl").write_text("corrupt")
    elif damage == "missing":
        (dist / "coordinare.whl").unlink()
    elif damage == "missing_manifest":
        (dist / "SHA256SUMS").unlink()
    elif damage in {"extra", "hidden"}:
        (dist / ("extra.whl" if damage == "extra" else ".extra.whl")).write_text("unverified")
    elif damage == "malformed":
        (dist / "SHA256SUMS").write_bytes(manifest + b"invalid checksum\n")
    elif damage == "duplicate":
        (dist / "SHA256SUMS").write_bytes(manifest + manifest)
    elif damage == "symlink":
        (dist / "coordinare.whl").rename(tmp_path / "coordinare.whl")
        (dist / "coordinare.whl").symlink_to(tmp_path / "coordinare.whl")
    elif damage == "directory":
        (dist / "extra").mkdir()
    steps = workflow()["jobs"]["publish-artifacts"]["steps"]
    verify = next(s for s in steps if s.get("id") == "verify")
    result = run_artifact_step(verify, tmp_path)
    assert (result.returncode == 0) == (damage == "none"), result.stdout + result.stderr
    if damage == "none":
        assert (dist / "SHA256SUMS").read_bytes() == manifest


@pytest.mark.parametrize("delete_fails", [False, True])
def test_draft_rerun_deletes_only_matching_assets_before_upload(tmp_path: Path, delete_fails: bool) -> None:
    prepare_release_dist(tmp_path)
    steps = workflow()["jobs"]["publish-artifacts"]["steps"]
    cleanup = next(s for s in steps if s.get("id") == "cleanup")
    assert cleanup["env"]["RELEASE_ID"] == "${{ needs.create-tag.outputs.release_id }}"
    script = cleanup["with"]["script"]
    harness = """
const assert = require('node:assert/strict');
const events = [];
const context = {repo: {owner: 'ViviDynamics', repo: 'coordinare'}};
const github = {
  paginate: async (method, params) => {
    assert.equal(params.release_id, Number(process.env.RELEASE_ID));
    assert.equal(method, github.rest.repos.listReleaseAssets);
    return ['coordinare.whl', 'SHA256SUMS', 'unrelated.txt'].map(name => ({id: name, name}));
  },
  rest: {repos: {
    listReleaseAssets: () => { throw new Error('must paginate'); },
    deleteReleaseAsset: async params => {
      await new Promise(resolve => setImmediate(resolve));
      if (DELETE_FAILS) throw new Error('deletion failed');
      events.push(params.asset_id);
    },
  }},
};
(async () => { SCRIPT })().then(() => {
  assert.deepEqual(events.sort(), ['SHA256SUMS', 'coordinare.whl']);
}).catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT", script).replace("DELETE_FAILS", json.dumps(delete_fails))
    result = subprocess.run(
        ["node", "-e", harness], cwd=tmp_path,
        env={**os.environ, "RELEASE_ID": str(RELEASE_ID)},
        capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is not delete_fails, result.stderr


def test_release_upload_preserves_build_manifest_and_only_uploads_verified_files(tmp_path: Path) -> None:
    manifest = prepare_release_dist(tmp_path)
    steps = workflow()["jobs"]["publish-artifacts"]["steps"]
    verify = next(s for s in steps if s.get("id") == "verify")
    result = run_artifact_step(verify, tmp_path)
    assert result.returncode == 0, result.stderr
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > uploaded.txt\n')
    gh.chmod(EXECUTABLE_MODE)
    upload = next(s for s in steps if s.get("id") == "upload")
    with patch.dict(os.environ, {"PATH": f"{tmp_path}:{os.environ['PATH']}"}):
        result = run_artifact_step(upload, tmp_path)
    assert result.returncode == 0, result.stderr
    expected = sorted(f"dist/{path.name}" for path in (tmp_path / "dist").iterdir())
    assert (tmp_path / "uploaded.txt").read_text().splitlines() == ["release", "upload", VERSION, *expected]
    assert (tmp_path / "dist/SHA256SUMS").read_bytes() == manifest


def test_staged_packages_have_tag_versions_pinned_siblings_and_legal_files(tmp_path: Path) -> None:
    projects = release_tools().prepare_projects(ROOT, tmp_path / "stage", VERSION, "ViviDynamics/coordinare")
    assert len(projects) == len(release_tools().PROJECTS)
    for project in projects:
        data = tomllib.loads((project / "pyproject.toml").read_text())
        assert data["project"]["version"] == VERSION
        assert "sources" not in data.get("tool", {}).get("uv", {})
        assert data["project"]["license"] == "LicenseRef-Elastic-License-2.0"
        for filename in ("LICENSE", "NOTICE"):
            assert (project / filename).read_bytes() == (ROOT / filename).read_bytes()
        for dependency in data["project"].get("dependencies", []):
            if dependency.startswith("coordinare-"):
                assert f"/releases/download/{VERSION}/" in dependency
                assert dependency.endswith(f"-{VERSION}-py3-none-any.whl")
    source = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in source["project"]["dynamic"]
    assert not source["tool"]["hatch"].get("metadata", {}).get("allow-direct-references", False)
    staged = tomllib.loads((projects[0] / "pyproject.toml").read_text())
    for filename in staged["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]:
        assert (projects[0] / filename).is_file(), f"Missing staged force-include source: {filename}"
        assert (projects[0] / filename).read_bytes() == (ROOT / filename).read_bytes()
    assert staged["tool"]["hatch"]["metadata"]["allow-direct-references"] is True
    assert VERSION not in (ROOT / "src/coordinare/_version.py").read_text()


@pytest.mark.parametrize("present", [True, False])
def test_staging_requires_arbitrary_root_force_include_files(tmp_path: Path, present: bool) -> None:
    source = tmp_path / "source"
    (source / "src/coordinare").mkdir(parents=True)
    for filename in ("LICENSE", "NOTICE"):
        (source / filename).write_text(filename)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "coordinare"\ndynamic = ["version"]\n'
        '[tool.hatch.build.targets.wheel.force-include]\n'
        '"release-resource.txt" = "coordinare/data/release-resource.txt"\n',
    )
    if present:
        (source / "release-resource.txt").write_text("release resource")
    tools = release_tools()
    with patch.dict(tools.PROJECTS, {"coordinare": "."}, clear=True):
        if present:
            projects = tools.prepare_projects(source, tmp_path / "stage", VERSION, "ViviDynamics/coordinare")
            assert (projects[0] / "release-resource.txt").read_text() == "release resource"
        else:
            with pytest.raises(FileNotFoundError, match=r"release-resource\.txt"):
                tools.prepare_projects(source, tmp_path / "stage", VERSION, "ViviDynamics/coordinare")


def test_every_example_config_is_included_in_wheels() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for example in ROOT.glob("config.example*.yaml"):
        assert included[example.name] == f"coordinare/data/{example.name}"


@pytest.mark.parametrize("version", ["latest", "2026.10.84;echo bad", "v2026.10.84"])
def test_invalid_release_versions_are_rejected(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        release_tools().prepare_projects(ROOT, tmp_path, version, "ViviDynamics/coordinare")


def test_optional_installer_is_copied_only_when_present(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "dist"
    destination.mkdir()
    tools = release_tools()
    tools.copy_installer(source, destination)
    assert not list(destination.iterdir())
    (source / "install.sh").write_text("#!/bin/sh\nexit 0\n")
    tools.copy_installer(source, destination)
    assert (destination / "install.sh").read_bytes() == (source / "install.sh").read_bytes()


def test_upload_failure_prevents_release_promotion(tmp_path: Path) -> None:
    """Exercise the real upload shell with a failing gh, without calling GitHub."""
    job = workflow()["jobs"]["publish-artifacts"]
    upload = next(s for s in job["steps"] if s.get("id") == "upload")
    manifest = prepare_release_dist(tmp_path)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(EXECUTABLE_MODE)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", upload["run"]],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path}:{os.defpath}:{os.environ['PATH']}", "RELEASE_VERSION": VERSION},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (tmp_path / "dist/SHA256SUMS").read_bytes() == manifest


def run_release_scripts(*jobs: str, published: bool = False, exists: bool = True) -> dict:
    scripts = []
    for job in jobs:
        script = workflow()["jobs"][job]["steps"][-1]["with"]["script"]
        script = script.replace("${{ needs.derive-version.outputs.version }}", VERSION)
        script = script.replace("${{ needs.derive-version.outputs.sha }}", "head")
        scripts.append(f"await (async () => {{ {script} }})();")
    harness = """
const outputs = {};
const counts = {created: 0, updated: 0, listed: 0};
const release = {id: RELEASE_NUMBER, tag_name: TAG, draft: DRAFT};
const releases = EXISTS ? [release] : [];
const context = {repo: {owner: 'ViviDynamics', repo: 'coordinare'}};
const core = {setOutput: (key, value) => {
  outputs[key] = value;
  if (key === 'release_id') process.env.RELEASE_ID = String(value);
}};
const github = {
  paginate: async (method, params) => {
    counts.listed++;
    return (await method(params)).data;
  },
  rest: {
    git: {getRef: async () => ({data: {object: {sha: 'head', type: 'commit'}}})},
    repos: {
      getCommit: async () => ({data: {sha: 'head', commit: {message: 'Release'}}}),
      listReleases: async () => ({data: [
        {id: RELEASE_NUMBER + 1, tag_name: 'another-tag', draft: true}, ...releases,
      ]}),
      getReleaseByTag: async () => {
        if (!releases.length || release.draft) throw Object.assign(new Error('not found'), {status: 404});
        return {data: release};
      },
      createRelease: async (params) => {
        if (releases.length) throw Object.assign(new Error('duplicate release'), {status: 422});
        if (!params.draft || params.make_latest !== 'false') throw new Error('must create draft');
        counts.created++;
        releases.push(release);
        return {data: release};
      },
      updateRelease: async (params) => {
        if (params.release_id !== release.id) throw new Error('wrong release id');
        if (params.draft !== false || params.make_latest !== 'true') throw new Error('must publish latest');
        release.draft = params.draft;
        counts.updated++;
      },
    },
  },
};
console.log = () => {};
(async () => { SCRIPTS })().then(() => process.stdout.write(JSON.stringify({outputs, counts, release})));
"""
    harness = harness.replace("RELEASE_NUMBER", str(RELEASE_ID)).replace("TAG", json.dumps(VERSION))
    harness = harness.replace("DRAFT", json.dumps(not published)).replace("EXISTS", json.dumps(exists))
    harness = harness.replace("SCRIPTS", "\n".join(scripts))
    result = subprocess.run(
        ["node", "-e", harness], check=True, text=True, capture_output=True,
        env={**os.environ, "RELEASE_VERSION": VERSION, "RELEASE_ID": str(RELEASE_ID)},
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("published", [True, False])
def test_existing_release_is_detected_without_recreating_it(published: bool) -> None:
    result = run_release_scripts("create-tag", published=published)
    assert result["outputs"]["published"] == str(published).lower()
    assert str(result["outputs"]["release_id"]) == str(RELEASE_ID)
    assert result["counts"]["created"] == 0
    assert result["counts"]["listed"] == 1


def test_failed_draft_rerun_reuses_release_and_promotes_it() -> None:
    result = run_release_scripts("create-tag", "create-tag", "publish-release")
    assert result["counts"]["created"] == 0
    assert result["counts"]["updated"] == 1
    assert result["release"]["draft"] is False


def test_new_release_id_flows_through_rerun_without_duplicates() -> None:
    result = run_release_scripts("create-tag", "create-tag", "publish-release", exists=False)
    assert result["counts"]["created"] == 1
    assert result["counts"]["updated"] == 1
    assert result["release"]["draft"] is False


def test_publish_promotes_draft_by_id_without_tag_lookup() -> None:
    result = run_release_scripts("publish-release")
    assert result["counts"] == {"created": 0, "updated": 1, "listed": 0}
    assert result["release"]["draft"] is False


@pytest.mark.parametrize("tag,normalized", [(LEGACY_TAG, NORMALIZED_VERSION), ("2026.01.1", "2026.1.1")])
def test_legacy_release_tags_normalize_metadata_but_preserve_download_path(
    tmp_path: Path, tag: str, normalized: str,
) -> None:
    projects = release_tools().prepare_projects(ROOT, tmp_path / "stage", tag, "ViviDynamics/coordinare")
    for project in projects:
        data = tomllib.loads((project / "pyproject.toml").read_text())
        assert data["project"]["version"] == normalized
        for dependency in data["project"].get("dependencies", []):
            if dependency.startswith("coordinare-"):
                assert f"/releases/download/{tag}/" in dependency
                assert dependency.endswith(f"-{normalized}-py3-none-any.whl")
    assert f'__version__ = "{normalized}"' in (projects[0] / "src/coordinare/_version.py").read_text()


def test_source_checkout_version_falls_back_to_embedded_build_version() -> None:
    with patch("coordinare.metrics.version", side_effect=PackageNotFoundError("coordinare")):
        assert _coordinare_version() == __version__


def test_installed_metadata_version_takes_priority() -> None:
    with patch("coordinare.metrics.version", return_value=VERSION):
        assert _coordinare_version() == VERSION
