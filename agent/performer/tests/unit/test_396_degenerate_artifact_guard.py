"""396: degenerate agent artifacts (repeated-token narration) must be refused
before commit, at every seam that lands a text artifact on a branch.

The incident: card #160's architect emitted 115,038 lines / 534 KB whose body
was `No.` x37,738, `Go.` x37,738, `Tool.` x16,011, `Proceed.` x8,807 and
`Now.` x8,801 — streamed narration that degenerated mid-run. It was committed
three times and pushed to an open PR as plan.md. Nothing between "the agent
wrote a file" and "the file is committed and pushed" looks at what the file
contains. The guard is mechanical: line repetition and size are the
signatures, with no model call and no tech-stack knowledge (#364).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.degeneracy import (
    DegenerateArtifactError,
    classify_file,
    classify_text,
)
from performer.main import Performance, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage
from performer.workflows.implementer.commits import revert_paths
from performer.workflows.implementer.salvage import salvage_failed_work
from performer.workspace import Stand as WorkspaceStand
from performer.workspace import commit_file, commit_files


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _git_stdout(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout.decode("utf-8")


def _incident_content() -> str:
    """The documented shape of website#193's plan.md (534 KB, 115,038 lines)."""
    parts = [
        "No.\n" * 37738,
        "Go.\n" * 37738,
        "Tool.\n" * 16011,
        "Proceed.\n" * 8807,
        "Now.\n" * 8801,
        "# Plan\n" + "".join(f"Real narration line {i} with detail.\n" for i in range(5943)),
    ]
    return "".join(parts)


def _small_degenerate() -> str:
    """600 lines, 200 of them the identical token — fires under defaults."""
    return "No.\n" * 200 + "".join(f"Line {i} says something specific.\n" for i in range(400))


# --------------------------------------------------------------------------
# the pure classifier
# --------------------------------------------------------------------------
def test_incident_shape_is_degenerate():
    verdict = classify_text(_incident_content())
    assert verdict.degenerate
    assert any("repetition" in r or "repeated" in r for r in verdict.reasons)
    assert any("bytes" in r for r in verdict.reasons)


def test_repetitive_file_without_size_trigger_is_degenerate():
    verdict = classify_text(_small_degenerate(), size_cap=None)
    assert verdict.degenerate


def test_unique_line_collapse_is_degenerate():
    content = ("loop\n" * 200 + "stuck\n" * 200 + "done\n" * 200)
    verdict = classify_text(content, size_cap=None)
    assert verdict.degenerate


def test_normal_plan_is_clean():
    content = "".join(
        f"# Section {i}\n\nDetail {i}: review the {i}th module boundary and its tests.\n"
        for i in range(600)
    )
    assert not classify_text(content).degenerate


def test_empty_content_is_clean():
    assert not classify_text("").degenerate
    assert not classify_text("\n \n").degenerate


def test_size_alone_fires_for_doc_artifacts():
    content = "".join(f"Varied line {i} of a large but honest document.\n" for i in range(20000))
    assert len(content.encode()) > 262_144
    assert classify_text(content).degenerate
    assert not classify_text(content, size_cap=None).degenerate


def test_repetition_below_the_line_floor_is_clean():
    assert not classify_text("No.\n" * 100).degenerate


def test_classify_file_skips_binary_and_missing(tmp_path: Path):
    (tmp_path / "bin.bin").write_bytes(b"\x00\x01\x02\xff")
    assert not classify_file(tmp_path / "bin.bin").degenerate
    assert not classify_file(tmp_path / "missing.md").degenerate


def test_classify_file_reads_text(tmp_path: Path):
    target = tmp_path / "plan.md"
    target.write_text(_small_degenerate(), encoding="utf-8")
    assert classify_file(target).degenerate


def test_error_carrying_paths():
    err = DegenerateArtifactError("docs/cards/x/plan.md", ("reason one",))
    assert "docs/cards/x/plan.md" in str(err)
    assert "reason one" in str(err)


# --------------------------------------------------------------------------
# architect_path: the incident seam — a degenerate plan fails the turn
# --------------------------------------------------------------------------
def _perf(tmp_path: Path) -> Performance:
    stand = Stand(path=tmp_path, branch="feat/test")
    stand.git_env = {}
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test")
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="architecting")


def _status_msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


@pytest.mark.asyncio
async def test_degenerate_inline_plan_fails_the_turn(tmp_path: Path):
    perf = _perf(tmp_path)
    perf.backend.get_status.return_value = BackendStatus(state="done", output=_small_degenerate())
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "error"
    assert perf.state == "error"
    assert perf.error_reason
    assert commit.await_count == 0


@pytest.mark.asyncio
async def test_degenerate_workspace_plan_fails_the_turn(tmp_path: Path):
    perf = _perf(tmp_path)
    folder = "docs/cards/test-card"
    (tmp_path / folder).mkdir(parents=True)
    (tmp_path / folder / "plan.md").write_text(_small_degenerate(), encoding="utf-8")
    perf.backend.get_status.return_value = BackendStatus(state="done", output="")
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "error"
    assert perf.state == "error"
    assert commit.await_count == 0


@pytest.mark.asyncio
async def test_degenerate_tasks_fails_the_turn(tmp_path: Path):
    perf = _perf(tmp_path)
    perf.backend.get_status.return_value = BackendStatus(
        state="done", output="# Plan\n\nA coherent plan body.\n---TASKS---\n" + _small_degenerate(),
    )
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "error"
    assert commit.await_count == 0


@pytest.mark.asyncio
async def test_healthy_plan_still_commits(tmp_path: Path):
    perf = _perf(tmp_path)
    perf.backend.get_status.return_value = BackendStatus(
        state="done", output="# Plan\n\nMigrate the model.\n---TASKS---\n- task one\n",
    )
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "plan_committed"
    assert commit.await_count >= 1


# --------------------------------------------------------------------------
# commit_file / commit_files: the doc-commit choke points refuse
# --------------------------------------------------------------------------
@pytest.fixture
def stand(tmp_path: Path) -> WorkspaceStand:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], tmp_path)
    work = tmp_path / "work"
    _git(["clone", str(remote), str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    _git(["checkout", "-b", "feat/x"], work)
    (work / "docs").mkdir()
    (work / "docs" / "seed.md").write_text("# seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)
    _git(["push", "-u", "origin", "feat/x"], work)
    s = WorkspaceStand(path=work, branch="feat/x")
    s.git_env = {}
    return s


@pytest.mark.asyncio
async def test_commit_file_refuses_degenerate_content(stand: WorkspaceStand):
    head_before = _head(stand)
    with pytest.raises(DegenerateArtifactError):
        await commit_file(stand, "docs/cards/x/plan.md", _small_degenerate(), "chore: plan")
    assert _head(stand) == head_before
    assert not (stand.path / "docs" / "cards" / "x" / "plan.md").exists()


@pytest.mark.asyncio
async def test_commit_files_refuses_degenerate_page(stand: WorkspaceStand):
    head_before = _head(stand)
    files: list[dict[str, str]] = [
        {"path": "docs/wiki/ok.md", "content": "# ok\n"},
        {"path": "docs/wiki/junk.md", "content": _small_degenerate()},
    ]
    with pytest.raises(DegenerateArtifactError):
        await commit_files(stand, files, "docs: batch")
    assert _head(stand) == head_before


@pytest.mark.asyncio
async def test_commit_file_accepts_healthy_content(stand: WorkspaceStand):
    await commit_file(stand, "docs/cards/x/plan.md", "# Plan\n\nMove fast.\n", "chore: plan")
    assert (stand.path / "docs" / "cards" / "x" / "plan.md").exists()


def _head(stand: WorkspaceStand) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=stand.path,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


# --------------------------------------------------------------------------
# salvage: a degenerate artifact is never carried onto the branch (#393 seam)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_salvage_excludes_degenerate_files(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "src").mkdir()
    (work / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    (work / "junk.md").write_text(_small_degenerate(), encoding="utf-8")

    commit = AsyncMock()
    ctx = SimpleNamespace(workspace=work, issue_number=1, push=AsyncMock())
    changed: dict[str, str] = {"junk.md": "added", "src/real.py": "added"}

    async def fake_changed(_ws: Path, _sha: str) -> dict[str, str]:
        return changed

    landed = await salvage_failed_work(ctx, since_sha="head", reason="test",
                                       _commit_paths=commit, _changed_paths_since=fake_changed)
    assert landed
    committed_paths = commit.call_args[0][1]
    assert "junk.md" not in committed_paths
    assert "src/real.py" in committed_paths


@pytest.mark.asyncio
async def test_salvage_of_only_degenerate_files_lands_nothing(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "junk.md").write_text(_small_degenerate(), encoding="utf-8")

    commit = AsyncMock()
    ctx = SimpleNamespace(workspace=work, issue_number=1, push=AsyncMock())

    async def fake_changed(_ws: Path, _sha: str) -> dict[str, str]:
        return {"junk.md": "added"}

    landed = await salvage_failed_work(ctx, since_sha="head", reason="test",
                                       _commit_paths=commit, _changed_paths_since=fake_changed)
    assert not landed
    commit.assert_not_called()
    assert ctx.push.await_count == 0


# --------------------------------------------------------------------------
# the implementer turn sweep: degenerate paths are reverted, never committed
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_turn_reverts_degenerate_paths(tmp_path: Path):
    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    # What the agent left behind: real work plus a degenerate artifact, both
    # IN scope so they survive the scope filter and reach the degeneracy sweep.
    (work / "src" / "more.py").write_text("y = 2\n", encoding="utf-8")
    (work / "src" / "junk.md").write_text(_small_degenerate(), encoding="utf-8")

    from performer.workflows.implementer.budgets import ImplementerBudgets
    from performer.workflows.implementer.driver import RunContext, run_turn
    from performer.workflows.implementer.models import TurnBrief

    toolkit = MagicMock()
    toolkit.run_agent_turn = AsyncMock(return_value={"exit_state": "done", "changed_paths": []})
    baseline_stub = SimpleNamespace(
        test_names=None, test_names_passed=None, pass_count=None, test_names_failed=[],
    )
    ctx = RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=work), score=None,
        budgets=ImplementerBudgets(), runner_kind="pytest", test_command="pytest",
        baseline=baseline_stub,  # type: ignore[arg-type]
    )

    brief = TurnBrief(
        kind="implement", persona_kind="IMPLEMENT", persona="implement the milestone",
        milestone_index=0, milestone_goal="g", scope_paths=["src/"],
        done_when="tests pass", forbidden_paths=[],
        failing_tests=[], failure_excerpt=None,
    )
    _result, _attempt, changed = await run_turn(ctx, brief, attempt_number=1)  # type: ignore[arg-type]

    assert "src/junk.md" not in changed
    assert "src/more.py" in changed
    assert not (work / "src" / "junk.md").exists()
    assert (work / "src" / "more.py").exists()
    reasons = [r for r in ctx.scope_reverts if r.get("kind") == "reverted_degenerate"]
    assert reasons and "src/junk.md" == reasons[0]["path"]


@pytest.mark.asyncio
async def test_run_turn_reports_failure_when_every_change_is_degenerate(
    tmp_path: Path,
):
    """An all-degenerate turn is a failed turn, not a completed one."""
    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    # the agent's only change is degenerate and in scope: the sweep empties
    # the change set, so the lane must see a failed turn and retry
    (work / "src" / "junk.md").write_text(_small_degenerate(), encoding="utf-8")

    from performer.workflows.implementer.budgets import ImplementerBudgets
    from performer.workflows.implementer.driver import RunContext, run_turn
    from performer.workflows.implementer.models import TurnBrief

    toolkit = MagicMock()
    toolkit.run_agent_turn = AsyncMock(return_value={"exit_state": "done", "changed_paths": []})
    baseline_stub = SimpleNamespace(
        test_names=None, test_names_passed=None, pass_count=None, test_names_failed=[],
    )
    ctx = RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=work), score=None,
        budgets=ImplementerBudgets(), runner_kind="pytest", test_command="pytest",
        baseline=baseline_stub,  # type: ignore[arg-type]
    )

    brief = TurnBrief(
        kind="implement", persona_kind="IMPLEMENT", persona="implement the milestone",
        milestone_index=0, milestone_goal="g", scope_paths=["src/"],
        done_when="tests pass", forbidden_paths=[],
        failing_tests=[], failure_excerpt=None,
    )
    result, attempt, changed = await run_turn(ctx, brief, attempt_number=1)  # type: ignore[arg-type]

    assert changed == {}
    assert result.exit_state == "error"
    assert attempt.exit_state == "error"
    assert attempt.failure_reason == "turn error"


# --------------------------------------------------------------------------
# Copilot round 1: secret redaction, bounded reads, staged-index bypasses
# --------------------------------------------------------------------------
def test_reasons_never_carry_the_repeated_line_verbatim() -> None:
    """A credential-bearing repeated line must not reach logs or responses."""
    secret = "API_KEY='sk-live-9f2c4d1e'\n"
    content = secret * 600 + "".join(f"Unique line {i}.\n" for i in range(20))
    verdict = classify_text(content)
    assert verdict.degenerate
    for reason in verdict.reasons:
        assert "sk-live-9f2c4d1e" not in reason
    assert any("sha256:" in r for r in verdict.reasons)


def test_classify_file_never_reads_beyond_the_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """size_cap=None (tree sweep) must stay bounded: prefix scan, no OOM."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    big = tmp_path / "big.md"
    big.write_text("No.\n" * 2000, encoding="utf-8")
    verdict = classify_file(big, size_cap=None, min_repetition_lines=50)
    assert verdict.degenerate
    assert any("sha256:" in r for r in verdict.reasons)


@pytest.mark.asyncio
async def test_commit_file_refuses_pre_staged_degenerate_content(stand: WorkspaceStand):
    """A degenerate file staged earlier must not ride a healthy commit."""
    (stand.path / "junk.md").write_text(_small_degenerate(), encoding="utf-8")
    _git(["add", "junk.md"], stand.path)
    head_before = _head(stand)

    with pytest.raises(DegenerateArtifactError) as excinfo:
        await commit_file(
            stand, "docs/cards/x/plan.md", "healthy plan content\n", "chore: plan",
        )
    assert "junk.md" in str(excinfo.value)
    assert _head(stand) == head_before


@pytest.mark.asyncio
async def test_salvage_unstages_excluded_degenerate_paths(tmp_path: Path):
    """An excluded path still in the index must not ride the salvage commit."""
    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], work)
    _git(["commit", "-m", "seed", "--no-gpg-sign"], work)
    (work / "junk.md").write_text(_small_degenerate(), encoding="utf-8")
    (work / "real.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "junk.md", "real.py"], work)

    commit = AsyncMock()
    ctx = SimpleNamespace(workspace=work, issue_number=1, push=AsyncMock())

    async def fake_changed(_ws: Path, _sha: str) -> dict[str, str]:
        return {"junk.md": "added", "real.py": "added"}

    landed = await salvage_failed_work(
        ctx, since_sha="head", reason="test",
        _commit_paths=commit, _changed_paths_since=fake_changed,
    )
    assert landed
    committed_paths = commit.call_args[0][1]
    assert "junk.md" not in committed_paths
    staged = _git_stdout(["diff", "--cached", "--name-only"], work)
    assert "junk.md" not in staged
    assert "real.py" in staged


@pytest.mark.asyncio
async def test_revert_paths_unstages_staged_new_file(tmp_path: Path):
    """A staged-but-never-committed path must leave the index when reverted."""
    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], work)
    _git(["commit", "-m", "seed", "--no-gpg-sign"], work)
    (work / "junk.md").write_text(_small_degenerate(), encoding="utf-8")
    _git(["add", "junk.md"], work)

    reverted = await revert_paths(work, ["junk.md"])

    assert reverted == ["junk.md"]
    assert not (work / "junk.md").exists()
    staged = _git_stdout(["diff", "--cached", "--name-only"], work)
    assert "junk.md" not in staged


@pytest.mark.asyncio
async def test_commit_file_refuses_when_staged_blob_differs_from_worktree(
    stand: WorkspaceStand,
):
    """The staged blob is classified, not the worktree copy."""
    (stand.path / "junk.md").write_text(_small_degenerate(), encoding="utf-8")
    _git(["add", "junk.md"], stand.path)
    # the agent "cleans up" the worktree after staging; the index still
    # holds the degenerate content a later commit would take
    (stand.path / "junk.md").write_text("healthy content\n", encoding="utf-8")
    head_before = _head(stand)

    with pytest.raises(DegenerateArtifactError):
        await commit_file(
            stand, "docs/cards/x/plan.md", "healthy plan content\n", "chore: plan",
        )
    assert _head(stand) == head_before


def test_classify_file_skips_oversized_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binary sniff runs before the size verdict (Copilot round 2)."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    big_binary = tmp_path / "asset.bin"
    big_binary.write_bytes(b"\x00\x01\x02" * 4096)
    verdict = classify_file(big_binary)
    assert not verdict.degenerate


@pytest.mark.asyncio
async def test_staged_blob_prefix_bounds_the_stream(tmp_path: Path):
    """Only *limit* bytes are buffered; the true size still comes back."""
    from performer.workflows.implementer.commits import staged_blob_prefix

    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], work)
    _git(["commit", "-m", "seed", "--no-gpg-sign"], work)
    body = _small_degenerate().encode("utf-8")
    (work / "junk.md").write_bytes(body)
    _git(["add", "junk.md"], work)

    blob = await staged_blob_prefix(work, "junk.md", 512)
    assert blob is not None
    size, prefix = blob
    assert size == len(body)
    assert len(prefix) == 512

    # and the size rule still fires from the true size, prefix-classified
    from performer import degeneracy

    verdict = degeneracy.classify_bytes(size, prefix, min_repetition_lines=50)
    assert verdict.degenerate
    missing = await staged_blob_prefix(work, "never-staged.md", 512)
    assert missing is None


# --------------------------------------------------------------------------
# Copilot round 5: streaming scan, architect reads, staged leftovers
# --------------------------------------------------------------------------
def test_streaming_scan_catches_repetition_beyond_the_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repetitive TAIL behind an ordinary prefix must still be refused."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    prefix = "".join(f"ordinary line {i:02d} of the document.\n" for i in range(20))
    assert len(prefix.encode()) > 512
    f = tmp_path / "big.md"
    f.write_text(prefix + "No.\n" * 2000, encoding="utf-8")
    verdict = classify_file(f, size_cap=None, min_repetition_lines=50)
    assert verdict.degenerate


def test_streaming_scan_skips_unique_rule_on_counter_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-line counter overflows the scan stays bounded, not wrong."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    monkeypatch.setattr(degeneracy, "_MAX_TRACKED_UNIQUE", 10)
    lines = "".join(f"unique line {i:04d} with words.\n" for i in range(60))
    f = tmp_path / "big.md"
    f.write_text(lines, encoding="utf-8")
    verdict = classify_file(f, size_cap=None, min_repetition_lines=50)
    assert not verdict.degenerate


def test_workspace_plan_refused_before_materialization(tmp_path: Path) -> None:
    """The architect seam classifies a workspace file before reading it."""
    from performer.status_paths import _workspace_file_refusal

    class _Perf:
        state = ""
        error_reason = ""
        session_id = "s"

    degenerate = tmp_path / "plan.md"
    degenerate.write_text(_small_degenerate(), encoding="utf-8")
    perf = _Perf()
    refusal = _workspace_file_refusal(perf, "plan", degenerate)  # type: ignore[arg-type]
    assert refusal is not None
    assert refusal.status == "error"

    clean = tmp_path / "clean.md"
    clean.write_text("A short, honest plan.\n", encoding="utf-8")
    assert _workspace_file_refusal(perf, "plan", clean) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_turn_unstages_degenerate_staged_leftovers(
    tmp_path: Path,
):
    """A degenerate blob staged but absent from `changed` cannot ride the commit."""
    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    # staged under the agent-state dir: the change filter drops it from
    # `changed`, yet the index-wide commit would still carry it (#396)
    (work / ".codex").mkdir()
    (work / ".codex" / "junk.md").write_text(_small_degenerate(), encoding="utf-8")
    _git(["add", ".codex/junk.md"], work)

    from performer.workflows.implementer.budgets import ImplementerBudgets
    from performer.workflows.implementer.driver import RunContext, run_turn
    from performer.workflows.implementer.models import TurnBrief

    toolkit = MagicMock()
    toolkit.run_agent_turn = AsyncMock(return_value={"exit_state": "done", "changed_paths": []})
    baseline_stub = SimpleNamespace(
        test_names=None, test_names_passed=None, pass_count=None, test_names_failed=[],
    )
    ctx = RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=work), score=None,
        budgets=ImplementerBudgets(), runner_kind="pytest", test_command="pytest",
        baseline=baseline_stub,  # type: ignore[arg-type]
    )

    brief = TurnBrief(
        kind="implement", persona_kind="IMPLEMENT", persona="implement the milestone",
        milestone_index=0, milestone_goal="g", scope_paths=["src/"],
        done_when="tests pass", forbidden_paths=[],
        failing_tests=[], failure_excerpt=None,
    )
    _result, attempt, _changed = await run_turn(ctx, brief, attempt_number=1)  # type: ignore[arg-type]

    staged_after = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=work, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert staged_after == ""
    assert any(r["kind"] == "unstaged_degenerate" and r["path"] == ".codex/junk.md"
               for r in ctx.scope_reverts)
    # with the leftover unstaged there is no healthy work to commit, so the
    # turn is a failure, not a completed milestone
    assert _result.exit_state == "error"
    assert attempt.exit_state == "error"


# --------------------------------------------------------------------------
# Copilot round 6: overflow heavy hitters, streamed staged blobs, boundaries
# --------------------------------------------------------------------------
def test_streaming_scan_survives_overflowing_unique_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heavy hitter appearing only after the counter filled is still caught."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    monkeypatch.setattr(degeneracy, "_MAX_TRACKED_UNIQUE", 50)
    prefix = "".join(f"unique line {i:04d} with words.\n" for i in range(60))
    f = tmp_path / "big.md"
    f.write_text(prefix + "No.\n" * 500, encoding="utf-8")
    verdict = classify_file(f, size_cap=None, min_repetition_lines=50)
    assert verdict.degenerate


def test_streaming_scan_bounds_a_newline_free_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file with no newline for megabytes must not buffer itself whole."""
    from performer import degeneracy

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    f = tmp_path / "minified.txt"
    f.write_text("x" * (4 * 1024 * 1024), encoding="utf-8")
    verdict = classify_file(f, size_cap=None, min_repetition_lines=50)
    assert not verdict.degenerate


@pytest.mark.asyncio
async def test_classify_staged_blob_streams_the_whole_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A repetitive tail behind an ordinary staged prefix is still refused."""
    from performer import degeneracy
    from performer.workflows.implementer.commits import classify_staged_blob

    monkeypatch.setattr(degeneracy, "HARD_READ_LIMIT", 512)
    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    prefix = "".join(f"ordinary line {i:02d} of the document.\n" for i in range(20))
    (work / "junk.md").write_text(prefix + "No.\n" * 2000, encoding="utf-8")
    _git(["add", "junk.md"], work)

    verdict = await classify_staged_blob(work, "junk.md", size_cap=None)
    assert verdict is not None
    assert verdict.degenerate


async def test_classify_staged_blob_skips_binary_before_the_size_cap(
    tmp_path: Path,
):
    """A large binary staged blob is skipped, not refused as degenerate text."""
    from performer.workflows.implementer.commits import classify_staged_blob

    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    (work / "asset.bin").write_bytes(b"\x00" * 300_000)
    _git(["add", "asset.bin"], work)

    verdict = await classify_staged_blob(work, "asset.bin")
    assert verdict is not None
    assert not verdict.degenerate


async def test_classify_staged_blob_size_cap_applies_to_text(tmp_path: Path):
    """An oversized text staged blob is refused from its true size."""
    from performer.workflows.implementer.commits import classify_staged_blob

    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    (work / "big.txt").write_text("x" * 300_000, encoding="utf-8")
    _git(["add", "big.txt"], work)

    verdict = await classify_staged_blob(work, "big.txt")
    assert verdict is not None
    assert verdict.degenerate
    assert "byte cap" in verdict.reasons[0]


async def test_staged_binary_with_nul_beyond_the_first_chunk_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A NUL past the first chunk still skips the size verdict."""
    from performer.workflows.implementer import commits
    from performer.workflows.implementer.commits import classify_staged_blob

    monkeypatch.setattr(commits, "_READ_CHUNK", 1024)
    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    (work / "asset.bin").write_bytes(b"A" * 2048 + b"\x00" * 300_000)
    _git(["add", "asset.bin"], work)

    verdict = await classify_staged_blob(work, "asset.bin")
    assert verdict is not None
    assert not verdict.degenerate


def test_classify_file_applies_the_cap_below_the_streaming_threshold(
    tmp_path: Path,
):
    """The size verdict is independent of the hard streaming boundary."""
    from performer import degeneracy

    big = tmp_path / "big.txt"
    big.write_text("x" * 300_000, encoding="utf-8")
    verdict = degeneracy.classify_file(big)
    assert verdict.degenerate
    assert "byte cap" in verdict.reasons[0]


@pytest.mark.asyncio
async def test_salvage_reverts_degenerate_paths_from_the_worktree(
    tmp_path: Path,
):
    """A degenerate artifact is removed from the tree, not just the index."""
    from performer.workflows.implementer import salvage as sal

    work = tmp_path / "repo"
    _git(["init", "-b", "main", str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)

    (work / "healthy.md").write_text("real work\n", encoding="utf-8")
    (work / "junk.md").write_text("No.\n" * 2000, encoding="utf-8")

    push = AsyncMock()
    ctx = SimpleNamespace(stand=SimpleNamespace(path=str(work)), workspace=work,
                          issue_number=7, push=push)
    ok = await sal.salvage_failed_work(
        ctx, since_sha="oldsha", reason="red was not observed",
        _commit_paths=AsyncMock(return_value="newsha"),
        _changed_paths_since=AsyncMock(
            return_value={"healthy.md": "added", "junk.md": "added"}
        ),
    )
    assert ok is True
    assert not (work / "junk.md").exists()
    assert (work / "healthy.md").read_text(encoding="utf-8") == "real work\n"
    push.assert_awaited_once()
