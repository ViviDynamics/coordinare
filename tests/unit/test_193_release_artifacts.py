from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinare.__main__ import _build_arg_parser
from coordinare.dashboard import assets
from coordinare.doctor import DoctorReport

ROOT = Path(__file__).resolve().parents[2]
VERSION = "2026.10.84"


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


def test_release_is_draft_until_assets_and_existing_publish_jobs_succeed() -> None:
    jobs = workflow()["jobs"]
    create = jobs["create-tag"]["steps"][-1]["with"]["script"]
    assert "draft: true" in create
    assert "make_latest: 'false'" in create
    # Draft releases do not create a ref. Image and package checkouts need a real tag.
    assert "github.rest.git.createRef" in create
    assert "refs/tags/" in create
    assert "published" in jobs["create-tag"]["outputs"]
    publish = jobs["publish-release"]
    assert {"create-tag", "release-artifacts", "tag-latest", "publish-chart"} <= set(publish["needs"])
    assert "release-artifacts" in jobs["tag-latest"]["needs"]
    script = publish["steps"][-1]["with"]["script"]
    assert "draft: false" in script
    assert "make_latest: 'true'" in script
    assert "always()" not in str(publish)


def test_release_build_uses_tag_and_attaches_checksums_and_provenance() -> None:
    job = workflow()["jobs"]["release-artifacts"]
    assert "published != 'true'" in job["if"]
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ needs.create-tag.outputs.version }}"
    build = next(s for s in steps if s.get("id") == "build")
    assert build["env"]["RELEASE_VERSION"] == "${{ needs.create-tag.outputs.version }}"
    assert "release_packages.py" in build["run"]
    assert "smoke_release.py" in str(steps)
    assert "uv tool install" in str(steps)
    smoke_step = next(s for s in steps if "smoke_release.py" in s.get("run", ""))
    assert "--with" not in smoke_step["run"]
    assert any("performer-*.whl" in s.get("run", "") for s in steps)
    attestation = next(s for s in steps if s.get("id") == "attest")
    assert attestation["uses"].startswith("actions/attest@")
    assert attestation["with"]["subject-path"] == "dist/*"
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["attestations"] == "write"
    upload = next(s for s in steps if s.get("id") == "upload")
    assert "bundle-path" in upload["env"]["ATTESTATION_BUNDLE"]
    assert "SHA256SUMS" in upload["run"]
    assert "sha256sum" in upload["run"]
    assert "gh release upload" in upload["run"]
    assert "dist/*" in upload["run"]
    assert steps.index(attestation) < steps.index(upload)
    assert "test_142_license_and_legal_posture.py" in str(workflow()["jobs"]["test"])


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
    assert VERSION not in (ROOT / "src/coordinare/_version.py").read_text()


def test_every_example_config_is_included_in_wheels() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for example in ROOT.glob("config.example*.yaml"):
        assert included[example.name] == f"coordinare/data/{example.name}"


@pytest.mark.parametrize("version", ["latest", "2026.10.84;echo bad", "v2026.10.84", "2026.01.1"])
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
    job = workflow()["jobs"]["release-artifacts"]
    upload = next(s for s in job["steps"] if s.get("id") == "upload")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/coordinare.whl").write_bytes(b"wheel")
    (tmp_path / "bundle.json").write_text("{}")
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", upload["run"]],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path}:{os.defpath}:{os.environ['PATH']}", "ATTESTATION_BUNDLE": str(tmp_path / "bundle.json"), "RELEASE_VERSION": VERSION},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    checksums = (tmp_path / "dist/SHA256SUMS").read_text()
    assert "coordinare.whl" in checksums and "provenance.json" in checksums
    for line in checksums.splitlines():
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((tmp_path / "dist" / name).read_bytes()).hexdigest()
    assert "SHA256SUMS" not in checksums


@pytest.mark.parametrize("published", [True, False])
def test_existing_release_is_detected_without_recreating_it(published: bool) -> None:
    script = workflow()["jobs"]["create-tag"]["steps"][-1]["with"]["script"]
    harness = """
const calls = [];
const context = {repo: {owner: 'ViviDynamics', repo: 'coordinare'}};
const core = {setOutput: (key, value) => calls.push([key, value])};
const github = {rest: {
  git: {getRef: async () => ({data: {object: {sha: 'head', type: 'commit'}}})},
  repos: {
    getCommit: async () => ({data: {sha: 'head'}}),
    getReleaseByTag: async () => ({data: {draft: DRAFT}}),
    createRelease: async () => {throw new Error('must not recreate a release');},
  },
}};
console.log = () => {};
(async () => { SCRIPT })().then(() => process.stdout.write(JSON.stringify(calls)));
""".replace("DRAFT", json.dumps(not published)).replace("SCRIPT", script)
    harness = harness.replace("${{ needs.derive-version.outputs.version }}", VERSION)
    harness = harness.replace("${{ needs.derive-version.outputs.sha }}", "head")
    result = subprocess.run(["node", "-e", harness], check=True, text=True, capture_output=True)
    assert ["published", str(published).lower()] in json.loads(result.stdout)
