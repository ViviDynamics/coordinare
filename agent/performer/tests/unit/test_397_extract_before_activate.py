"""#397: the env-cache extraction must finish before anything depends on it.

The devenv profile extracts the cache's debs into a container-local lib dir the
first time any bash sources it. In a fresh implementer container that first
bash was ``_activate_env_cache``'s 30s env snapshot. Extraction takes ~36s
uncontended (52-110s under load), so the snapshot timed out, returned ``{}``,
and LEFT ITS CHILD RUNNING with the extraction lock held. The very next step,
``_start_env_cache_services``, sourced the profile, found a seconds-old lock and
no lib dir, and launched postgres without ``LD_LIBRARY_PATH``. That postgres
needs ``libicui18n.so.76`` from the extracted libs and cannot start; 180s later
services-start exits 75 and the card is held.

Three things were wrong and each has a test here:

1. Extraction was never a step of its own. It happened as a side effect of a
   30s snapshot that could not afford it. ``_extract_env_cache_libs`` runs it
   to completion, with its own budget, BEFORE the snapshot.
2. The snapshot did not kill the child it timed out. An orphaned extractor
   holding a lock is the worst state to leave behind.
3. The timeout log carried ``error=`` with nothing after it, because
   ``str(asyncio.TimeoutError())`` is empty. 45 such lines in the performer
   logs said nothing about what happened.

MUTATIONS THAT MUST FAIL A TEST HERE:
  M1  drop ``start_new_session=True`` / the kill in ``_activate_env_cache``'s
      timeout path        -> test_activation_timeout_kills_its_child
  M2  drop the ``_extract_env_cache_libs`` call from ``prepare_workspace``,
      or move it after activation -> test_extraction_runs_before_activation
  M3  drop the kill in ``_extract_env_cache_libs``'s timeout path
                          -> test_extraction_timeout_kills_its_child
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _hanging_proc() -> MagicMock:
    """A subprocess whose communicate()/wait() never return on their own."""
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = None

    async def _hang(*_a, **_k):
        await asyncio.sleep(9999)

    proc.communicate = _hang
    proc.wait = _hang
    return proc


class TestActivationTimeoutReapsItsChild:
    @pytest.mark.asyncio
    async def test_activation_timeout_kills_its_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M1: a timed-out snapshot must reap the extractor it spawned, and it
        must have spawned it in its own session so the whole group dies."""
        import performer.workspace as ws

        (tmp_path / "activate.sh").write_text("export X=1\n")
        monkeypatch.setattr(ws, "_ACTIVATE_TIMEOUT_S", 0.05)
        proc = _hanging_proc()
        kill = AsyncMock()
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc) as exec_,
            patch("performer.workspace._kill_cache_process_group", kill),
        ):
            result = await ws._activate_env_cache(str(tmp_path))

        assert result == {}, "a timed-out snapshot still yields an empty delta"
        assert exec_.call_args.kwargs.get("start_new_session") is True, (
            "without its own session, killing the shell leaves dpkg-deb running"
        )
        kill.assert_awaited_once_with(proc)


class TestExtractionRunsFirst:
    @pytest.mark.asyncio
    async def test_extraction_runs_before_activation(self, tmp_path: Path) -> None:
        """M2: extract, THEN snapshot, THEN start services. Any other order
        re-creates the failure: a snapshot that pays for extraction times out,
        and a services start that runs before publication launches postgres
        without its libraries."""
        import performer.workspace as ws
        from tests.unit.test_workspace import _score

        order: list[str] = []

        async def _rec(name):
            async def _f(*a, **k):
                order.append(name)
                return True if name == "extract" else {}
            return _f

        with (
            patch("performer.workspace._run_git", new=AsyncMock(return_value=(0, ""))),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("performer.workspace._extract_env_cache_libs", new=await _rec("extract")) as ext,
            patch("performer.workspace._activate_env_cache", new=await _rec("activate")),
            patch("performer.workspace._start_env_cache_services", new=await _rec("services")),
        ):
            await ws.clone_repository(_score(env_cache_path="/devenv/website-x"))

        assert order == ["extract", "activate", "services"], order
        del ext


class TestExtractionIsItsOwnStep:
    @pytest.mark.asyncio
    async def test_extraction_skips_without_a_cache(self) -> None:
        import performer.workspace as ws

        with patch("performer.workspace.asyncio.create_subprocess_exec") as exec_:
            assert await ws._extract_env_cache_libs("") is False
        exec_.assert_not_called()

    @pytest.mark.asyncio
    async def test_extraction_returns_true_when_the_profile_completes(
        self, tmp_path: Path
    ) -> None:
        import performer.workspace as ws

        proc = MagicMock()
        proc.pid = 7
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc) as exec_:
            assert await ws._extract_env_cache_libs(str(tmp_path)) is True
        env = exec_.call_args.kwargs.get("env") or {}
        assert env.get("_DEVENV_SKIP_SERVICES") == "1", (
            "extraction must not also start services; that is a later, explicit step"
        )
        assert "_DEVENV_SOURCED" not in env, (
            "the performer process already sourced the profile; the guard must be "
            "cleared or this shell extracts nothing"
        )
        assert exec_.call_args.kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_extraction_timeout_kills_its_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M3: the extraction budget is generous, but when it is exceeded the
        extractor is reaped, not orphaned."""
        import performer.workspace as ws

        monkeypatch.setattr(ws, "_EXTRACT_TIMEOUT_S", 0.05)
        proc = _hanging_proc()
        kill = AsyncMock()
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc),
            patch("performer.workspace._kill_cache_process_group", kill),
        ):
            assert await ws._extract_env_cache_libs(str(tmp_path)) is False
        kill.assert_awaited_once_with(proc)

    def test_extraction_budget_covers_the_measured_extraction(self) -> None:
        """Measured on the website cache (729 debs, 1.1 GB): 36s uncontended,
        52s with one neighbour, 110s with two shells contending. The budget
        must clear the contended figure with room for a loaded host."""
        import performer.workspace as ws

        assert ws._EXTRACT_TIMEOUT_S >= 300
        assert ws._ACTIVATE_TIMEOUT_S == 30.0, (
            "the snapshot budget is unchanged on purpose: extraction no longer "
            "happens inside it, so 30s is plenty and the failure mode is gone"
        )
