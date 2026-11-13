"""The clean consumer's {repo} is a git checkout of the pinned tree, as the installer's and implementer jobs' are.

Dettmore e2e-83: the installer ran on a git clone, the clean consumer on a plain tree, so the skills-wiring and
quality-guard checks failed with "not a git repository".
"""
from __future__ import annotations

import subprocess

import pytest
from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_restore import (
    initialize_source_repository,
    restore_execution_bundle,
)

from tests.unit.test_353_execution_source import COMMIT, FILES, bundle_file, entries_for


def git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout


def test_the_restored_source_is_a_clean_detached_checkout_of_the_pinned_tree(tmp_path):
    path, identity = bundle_file(tmp_path, {**FILES, ".gitignore": (b"asset.bin\n", "100644")})
    repo = tmp_path / "repo"
    restore_execution_bundle(path, repo, expected=ExecutionSourceIdentity.model_validate(identity), review_inputs={})
    assert git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == ""  # detached, like the installer's clone
    assert COMMIT in git(repo, "log", "-1", "--format=%s")
    # A tracked file that .gitignore matches is still part of the tree.
    assert "asset.bin" in git(repo, "ls-files").split()
    staged = {line.split("\t")[1]: tuple(line.split("\t")[0].split()[:2])
              for line in git(repo, "ls-files", "--stage").splitlines()}
    assert staged == {entry["path"]: (entry["mode"], entry["object_id"]) for entry in entries_for(
        {**FILES, ".gitignore": (b"asset.bin\n", "100644")})}
    assert git(repo, "remote") == ""


def test_a_tree_that_differs_from_the_bundle_record_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="differs from the pinned tree"):
        initialize_source_repository(repo, {"a": ("100644", "0" * 40)}, COMMIT)


def test_without_git_the_plain_tree_stays(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert initialize_source_repository(tmp_path, {}, COMMIT) is False
    assert not (tmp_path / ".git").exists()
