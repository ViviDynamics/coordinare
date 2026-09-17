"""Spec 134 — fixtures: whole-lifecycle work items + board seeding.

A fixture is a self-contained, from-scratch task: an issue (title + body), a
starting repo state (files seeded on ``main``, including the real pytest suite that
expresses acceptance), an optional ``solution_files`` set the stubbed performer
applies to make CI pass (deterministic integration test), and a planted
``ground_truth`` marker consumed later by spec 135 (unused in Phase 1).

The manifest is a YAML file with inline file contents so a single-file fixture set
needs no external repo. Whole-lifecycle fixtures for real runs live in the sibling
``ViviDynamics/conductor-bench`` repo and are referenced by a manifest path.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel

if TYPE_CHECKING:
    from coordinare.services.fake_github import FakeGitHubService


class Fixture(BaseModel):
    id: str
    title: str
    body: str
    base_files: dict[str, str]
    solution_files: dict[str, str] = {}
    head_ref: str = ""
    ground_truth: str = ""
    # Spec 135: the planted right terminal outcome. "merged" = correct work must
    # merge; "blocked" = the card must NOT merge (adversarial fixtures — e.g. a
    # planted vulnerability the pipeline is expected to hold). Default preserves
    # the meaning of every pre-135 manifest.
    expected_final_state: Literal["merged", "blocked"] = "merged"

    def branch(self) -> str:
        return self.head_ref or f"bench/{self.id}"


def load_manifest(path: str | Path) -> list[Fixture]:
    """Load a YAML manifest: ``{fixtures: [ {id, title, body, base_files, ...} ]}``."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return [Fixture(**f) for f in raw.get("fixtures", [])]


def tiny_fixture() -> Fixture:
    """The built-in cheap fixture for the deterministic integration test.

    Task: implement ``multiply``. ``main`` ships the acceptance test (which fails
    until implemented); ``solution_files`` supplies the passing implementation the
    stubbed performer applies.
    """
    return Fixture(
        id="tiny-multiply",
        title="Implement multiply()",
        body="Add a multiply(a, b) function to calc.py so the acceptance test passes.",
        base_files={
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_multiply.py": (
                "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n"
            ),
        },
        solution_files={
            "calc.py": "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
        },
        ground_truth="calc.multiply returns a*b; test_multiply passes",
    )


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        msg = f"git step failed: {' '.join(cmd)}\n{r.stderr}"
        raise RuntimeError(msg)


def materialize_repo(fixtures: list[Fixture], dest_dir: str | Path) -> Path:
    """Create a bare repo whose ``main`` holds every fixture's ``base_files``.

    Returns the bare-repo path. All fixtures share one repo (coordinare operates on a
    single repo); fixture authors keep file paths distinct to avoid collisions.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    seed = Path(tempfile.mkdtemp(prefix="bench-seed-", dir=dest))
    _run(["git", "init", "-b", "main", "."], seed)
    for fx in fixtures:
        for rel, content in fx.base_files.items():
            target = seed / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    _run(["git", "add", "-A"], seed)
    _run(["git", "-c", "user.email=bench@local", "-c", "user.name=bench", "commit", "-m", "seed"], seed)

    bare = dest / "repo.git"
    # -b main so the bare's HEAD points at main (not the git default "master");
    # otherwise fresh clones check out an unborn branch and lose main's files.
    _run(["git", "init", "--bare", "-b", "main", str(bare)], dest)
    _run(["git", "remote", "add", "origin", str(bare)], seed)
    _run(["git", "push", "origin", "main"], seed)
    return bare


def apply_solution_branch(
    bare_repo: str | Path, branch: str, files: dict[str, str], work_dir: str | Path, base: str = "main",
) -> None:
    """Create ``branch`` off ``base`` in the bare repo with ``files`` applied and push.

    Simulates the implementer performer's push in a deterministic (stubbed) run: a
    real commit so the fake's real-pytest CI and real squash-merge operate on it.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    clone = Path(tempfile.mkdtemp(prefix="bench-sol-", dir=work))
    _run(["git", "clone", "--quiet", str(bare_repo), str(clone)], work)
    # Branch explicitly off origin/<base> so the new branch carries base's files
    # regardless of what the clone's default HEAD resolved to.
    _run(["git", "checkout", "-b", branch, f"origin/{base}"], clone)
    for rel, content in files.items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _run(["git", "add", "-A"], clone)
    _run(["git", "-c", "user.email=bench@local", "-c", "user.name=bench", "commit", "-m", f"implement {branch}"], clone)
    _run(["git", "push", "--quiet", "origin", branch], clone)
    shutil.rmtree(clone, ignore_errors=True)


def seed_board(fake: FakeGitHubService, fixtures: list[Fixture], *, start_status: str = "TODO") -> None:
    """Register one backlog card per fixture on the fake board."""
    for i, fx in enumerate(fixtures, start=1):
        fake.seed_card(
            f"PVTI_{i}",
            title=fx.title,
            body=fx.body,
            status=start_status,
            issue_number=i,
            fixture_id=fx.id,
        )
