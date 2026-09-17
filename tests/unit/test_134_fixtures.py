"""Spec 134 — fixtures: manifest load, bare-repo materialize, board seeding (US4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare.bench.fixtures import (
    load_manifest,
    materialize_repo,
    seed_board,
    tiny_fixture,
)
from coordinare.services.fake_github import FakeGitHubService


def test_tiny_fixture_is_well_formed() -> None:
    fx = tiny_fixture()
    assert fx.id == "tiny-multiply"
    assert "test_multiply.py" in fx.base_files
    assert "multiply" in fx.solution_files["calc.py"]
    assert fx.branch() == "bench/tiny-multiply"


def test_load_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "fixtures:\n"
        "  - id: f1\n"
        "    title: Task one\n"
        "    body: do one\n"
        "    base_files:\n"
        "      a.py: \"x = 1\\n\"\n",
    )
    fixtures = load_manifest(manifest)
    assert len(fixtures) == 1
    assert fixtures[0].id == "f1"
    assert fixtures[0].base_files["a.py"] == "x = 1\n"


def test_materialize_repo_seeds_main(tmp_path: Path) -> None:
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    assert bare.exists()
    # main carries the fixture's base files
    r = subprocess.run(
        ["git", "show", "main:test_multiply.py"],
        cwd=str(bare), capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    assert "test_multiply" in r.stdout


async def test_seed_board_places_cards_in_backlog(tmp_path: Path) -> None:
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    fake = FakeGitHubService(bare_repo_path=bare, work_dir=tmp_path / "work")
    seed_board(fake, [fx])
    board = await fake.poll_board()
    assert board["snapshot"]["TODO"] == ["PVTI_1"]
    assert board["titles"]["PVTI_1"] == "Implement multiply()"
