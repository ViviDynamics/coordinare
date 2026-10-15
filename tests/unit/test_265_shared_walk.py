"""The image walker uses spec 234's anchored records and digest algorithms."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from coordinare_service_inference.cache_provenance import WalkRefusedError, walk_summary

from coordinare.services.env_provenance import DEFAULT_LIMITS, ProvenanceScope, stat_digest_of
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_verifier import VerifierSpec, build_verifier_pod_manifest

ACTIVATE = "export PATH=$DEVENV/bin:$PATH\n"


def test_shared_walker_matches_daemon_stat_and_hashes_canonical_content(tmp_path: Path) -> None:
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "activate.sh").write_text(ACTIVATE)
    result = walk_summary(cache, ACTIVATE, max_entries=100, max_bytes=1024)
    assert result["stat_digest"] == stat_digest_of(cache, ProvenanceScope((tmp_path,)))
    mode = (cache / "activate.sh").stat().st_mode & 0o7777
    row = ["activate.sh", "file", mode, len(ACTIVATE), hashlib.sha256(ACTIVATE.encode()).hexdigest(), None]
    expected = hashlib.sha256((json.dumps(row, separators=(",", ":")) + "\n").encode()).hexdigest()
    assert result["content_digest"] == expected
    os.chmod(cache / "activate.sh", mode)
    again = walk_summary(cache, ACTIVATE, max_entries=100, max_bytes=1024)
    assert again["content_digest"] == result["content_digest"]
    assert again["stat_digest"] != result["stat_digest"]
    assert result["entry_count"] == 1
    assert result["total_bytes"] == len(ACTIVATE)


@pytest.mark.parametrize("failure", ["entries", "bytes", "links"])
def test_shared_walker_reports_bounds_without_partial_success(tmp_path: Path, failure: str) -> None:
    (tmp_path / "activate.sh").write_text(ACTIVATE)
    if failure == "links":
        (tmp_path / "escape").symlink_to("../outside")
    with pytest.raises(WalkRefusedError):
        walk_summary(tmp_path, ACTIVATE, max_entries=0 if failure == "entries" else 100,
                     max_bytes=0 if failure == "bytes" else 1024)


@pytest.mark.parametrize("walk_only", [True, False])
def test_real_pod_command_emits_a_bounded_summary(tmp_path: Path, walk_only: bool) -> None:
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "activate.sh").write_text(ACTIVATE)
    spec = VerifierSpec(image="image", cache_claim="claim", cache_subpath="sym", mount_path=str(cache),
                        verify_text="exit 99\n" if walk_only else "exit 0\n", project_files={},
                        activate_text=ACTIVATE, walk_only=walk_only)
    pod = build_verifier_pod_manifest(spec, name="test", namespace="test")
    container = pod["spec"]["containers"][0]
    env = {**os.environ, **{item["name"]: item["value"] for item in container["env"]},
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(container["command"], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    if walk_only:
        parsed = parse_summary(result.stdout, ACTIVATE, DEFAULT_LIMITS)
        assert parsed["entry_count"] == 1
        assert parsed["inputs"]["activate_sh"] == ACTIVATE
    else:
        assert result.stdout == ""


def test_verify_output_is_isolated_from_the_subsequent_walk(tmp_path: Path) -> None:
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "activate.sh").write_text(ACTIVATE)
    verify = 'printf "changed" > "$DEVENV/activate.sh"\nprintf "verify finished\\n"\n'
    spec = VerifierSpec(image="image", cache_claim="claim", cache_subpath="sym", mount_path=str(cache),
                        verify_text=verify, project_files={}, activate_text=ACTIVATE)
    container = build_verifier_pod_manifest(spec, name="test", namespace="test")["spec"]["containers"][0]
    env = {**os.environ, **{item["name"]: item["value"] for item in container["env"]},
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(container["command"], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "verify finished\n"
    assert (cache / "activate.sh").read_text() == "changed"
    # The local command test has no read only mount. Even here, a later walk sees
    # the changed content and refuses it, rather than freezing a pre-verify result.
    walk = build_verifier_pod_manifest(replace(spec, walk_only=True, verify_text="", project_files={}),
                                       name="walk", namespace="test")["spec"]["containers"][0]
    env.update({item["name"]: item["value"] for item in walk["env"]})
    walked = subprocess.run(walk["command"], env=env, text=True, capture_output=True, check=False)
    with pytest.raises(ValueError, match=r"activate\.sh"):
        parse_summary(walked.stdout, ACTIVATE, DEFAULT_LIMITS)
