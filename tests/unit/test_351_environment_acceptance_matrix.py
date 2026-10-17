"""Executable contract matrix using synthetic tools; no language installs or network.

Ecosystem names, layouts and constraints are fixture data. Every case uses the same
shared validator, declarative installer, clean scratch state and check runner.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest
from coordinare_service_inference.environment.runner import run_manifest

MATRIX = [
    ("ruby-rails", "Gemfile", "ruby", "3.4.2", "rails", "8.0", ""),
    ("rust", "Cargo.toml", "rustc", "1.90", "application", "1.0", ""),
    ("go", "go.mod", "go", "1.25", "application", "1.0", ""),
    ("unfamiliar", "construction.notes", "quuxforge", "17.4", "quuxstdlib", "9", ""),
    ("monorepo", "apps/api/build guide.txt", "quuxforge", "17.4", "quuxstdlib", "9", "apps/api"),
]


def accepted_fixture(repo: Path, case: tuple, *, wrong_version: bool = False) -> EnvironmentManifest:
    name, source, tool, version, library, library_version, subdir = case
    evidence = f"Use {tool} {version}; load {library} {library_version}; run from {subdir or '.'}."
    source_file = repo / source
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(evidence)
    state = {"tool": tool, "version": "wrong" if wrong_version else version,
             "library": library, "library_version": library_version}
    program = (
        "import json,pathlib,sys\n"
        "state=json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())\n"
        "assert state[sys.argv[1]]==sys.argv[2], state\n"
    )
    installer = (
        "import pathlib,sys\n"
        "root=pathlib.Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True)\n"
        "(root/'fixture-tool.py').write_text(sys.argv[2])\n"
        "(root/'fixture-tool.json').write_text(sys.argv[3])\n"
    )
    cwd = "{repo}" + ("/" + subdir if subdir else "")
    data = {
        "schema_version": 1, "symphony_name": name, "source_commit": "b" * 40,
        "inputs": {source: hashlib.sha256(evidence.encode()).hexdigest()},
        "requirements": [
            {"id": "runtime", "description": f"{tool} {version}", "source": source, "checks": ["version"]},
            {"id": "loadability", "description": f"{library} {library_version}", "source": source,
             "checks": ["load", "library-version"]},
        ],
        "install": [{"id": "install", "argv": [sys.executable, "-c", installer,
                    "{cache}/bin", program, json.dumps(state)], "cwd": cwd}],
        "checks": [
            {"id": "version", "argv": [sys.executable, "{cache}/bin/fixture-tool.py", "version", version], "cwd": cwd},
            {"id": "load", "argv": [sys.executable, "{cache}/bin/fixture-tool.py", "library", library], "cwd": cwd},
            {"id": "library-version", "argv": [sys.executable, "{cache}/bin/fixture-tool.py", "library_version", library_version], "cwd": cwd},
            {"id": "working-directory", "argv": [sys.executable, "-c",
                "import pathlib,sys;assert pathlib.Path.cwd()==pathlib.Path(sys.argv[1])", cwd], "cwd": cwd},
        ],
        "activation": {"path": ["{cache}/bin"], "env": {"FIXTURE_ROOT": "{cache}"}},
        "services": [], "empty_reason": None, "policy_version": "351-v1",
    }
    manifest = EnvironmentManifest.model_validate(data)
    accept(manifest)
    return manifest


def accept(manifest: EnvironmentManifest) -> None:
    """Bind fixture acceptance after every execution-affecting mutation."""
    # Acceptance is deterministic test data, not a live independent model review.
    assessment = CoverageAssessment.model_validate({
        "schema_version": 1, "manifest_digest": manifest.digest(), "source_commit": manifest.source_commit,
        "symphony_name": manifest.symphony_name, "verdict": "accept", "rationale": "Fixture requirements cover documented constraints",
        "sources": list(manifest.inputs), "inputs": manifest.inputs, "policy_version": "351-v1",
    })
    assert assessment.manifest_digest == manifest.digest()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", MATRIX, ids=[case[0] for case in MATRIX])
async def test_install_then_fresh_consumer_checks_the_same_contract(tmp_path: Path, case: tuple) -> None:
    repo, cache = tmp_path / "repo", tmp_path / "cache"
    repo.mkdir()
    cache.mkdir()
    manifest = accepted_fixture(repo, case)
    digest = manifest.digest()
    install_scratch, clean_scratch = tmp_path / "install-state", tmp_path / "consumer-state"
    install_scratch.mkdir()
    clean_scratch.mkdir()
    installed = await run_manifest(manifest, cache=cache, repo=repo, scratch=install_scratch, install=True)
    assert installed.passed
    # Consumer verification gets no installation-only process results or scratch state.
    consumed = await run_manifest(manifest, cache=cache, repo=repo, scratch=clean_scratch)
    assert consumed.passed
    assert installed.manifest_digest == consumed.manifest_digest == digest
    assert {result.id for result in consumed.results} == {check.id for check in manifest.checks}
    assert all(result.exit_code == 0 for result in consumed.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong-version", "false", "missing-binary", "service-health"])
async def test_accepted_required_failures_never_establish_readiness(tmp_path: Path, failure: str) -> None:
    repo, cache = tmp_path / "repo", tmp_path / "cache"
    repo.mkdir()
    cache.mkdir()
    manifest = accepted_fixture(repo, MATRIX[3], wrong_version=failure == "wrong-version")
    data = manifest.model_dump()
    if failure in {"false", "missing-binary"}:
        data["checks"].append({"id": "must-pass", "argv": ["false"] if failure == "false"
                               else [str(tmp_path / "not-installed")]})
    if failure == "service-health":
        data["services"] = [{"id": "declared-service",
            "init": {"id": "init", "argv": [sys.executable, "-c", "from pathlib import Path;Path('initialized').touch()"], "cwd": "{scratch}"},
            "start": {"id": "start", "argv": [sys.executable, "-c", "import time;time.sleep(30)"]},
            "health": {"id": "health", "argv": ["false"]},
            "stop": {"id": "stop", "argv": [sys.executable, "-c", "pass"]}}]
    manifest = EnvironmentManifest.model_validate(data)
    accept(manifest)
    report = await run_manifest(manifest, cache=cache, repo=repo, scratch=tmp_path, install=True)
    assert not report.passed
    by_id = {result.id: result for result in report.results}
    failed_id = {"wrong-version": "version", "false": "must-pass", "missing-binary": "must-pass",
                 "service-health": "health"}[failure]
    assert by_id[failed_id].required and by_id[failed_id].status == "fail"
    if failure == "service-health":
        assert (tmp_path / "initialized").exists()
        assert by_id["version"].status == "not_run"
        assert by_id["stop"].status == "pass"
