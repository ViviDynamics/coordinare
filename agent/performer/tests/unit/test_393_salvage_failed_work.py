"""393: a failed milestone must leave its work on the branch.

The branch is already deterministic (`coordinare/{card_id}/{slug}`) and existing
branches are already preserved rather than clobbered, so the next container
clones the same branch and can edit what the last one left instead of deriving
it from nothing.

There was never anything on it, because nothing was committed when a milestone
failed:

    driver._feature
      529  outcome = await _red_phase(...)   # raises MilestoneFailed at :485
      537  await _commit(ctx, outcome.changed, "test(#N): failing tests for ...")

The commit sits AFTER the red phase, and `ctx.push()` sits upstream of the
handler that catches the exception. So a tests turn that wrote a spec and
failed its red judgement discarded the file and pushed nothing.

Measured on card #160: two complete runs, six minutes of writing each, the
IDENTICAL verdict both times -- because both started from an empty branch and
rewrote the same file. No memory between attempts.

A salvaged commit is NOT a spec-171 resume credit; see
``test_395_salvage_is_not_resume_credit`` for why counting it would turn this
exact card into a silent false green.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.workflows.implementer.salvage import (
    SALVAGE_PREFIX,
    salvage_message,
    should_salvage,
)


# --- what qualifies -----------------------------------------------------------

def test_a_turn_that_changed_files_is_salvaged():
    assert should_salvage({"spec/a_spec.rb": "added"}) is True


def test_a_turn_that_changed_nothing_is_not():
    """Nothing to commit, and an empty commit would move the head while
    carrying no work -- which would make the #390 budget read progress that
    did not happen."""
    assert should_salvage({}) is False
    assert should_salvage(None) is False


def test_build_artifacts_alone_do_not_qualify():
    """A turn whose only output is a cache directory produced nothing worth
    carrying, and committing it would still move the head."""
    assert should_salvage({"__pycache__/x.pyc": "added", ".pytest_cache/v": "added"}) is False


def test_real_work_alongside_artifacts_still_qualifies():
    assert should_salvage({"spec/a_spec.rb": "added", "__pycache__/x.pyc": "added"}) is True


# --- the message the next turn reads ------------------------------------------

def test_the_message_says_it_is_unfinished():
    m = salvage_message(7, "red was not observed: the tests did not fail for the right reason")
    assert m.startswith(SALVAGE_PREFIX), m
    assert "#7" in m


def test_the_message_carries_the_reason():
    """The next attempt must know WHY, or it re-derives the same file and
    reproduces the same verdict -- which is exactly what card #160 did twice."""
    m = salvage_message(7, "the tests did not fail for the right reason")
    assert "did not fail for the right reason" in m


def test_the_message_is_bounded():
    """Reasons carry a runner's raw output tail, which is unbounded."""
    m = salvage_message(7, "x" * 50_000)
    assert len(m) < 2000, len(m)


def test_a_missing_reason_still_produces_a_usable_message():
    for empty in ("", None, "   "):
        m = salvage_message(3, empty)
        assert m.startswith(SALVAGE_PREFIX)
        assert len(m.strip()) > len(SALVAGE_PREFIX)


def test_the_message_has_no_issue_number_when_there_is_none():
    assert "#0" not in salvage_message(0, "reason")


# --- the workflow path --------------------------------------------------------


class _FakeCtx(SimpleNamespace):
    """Mirrors the real ``RunContext``: ``workspace`` is derived, never stored.

    395: these fakes stored ``stand.path`` and nothing else, so when salvage
    moved to ``ctx.workspace`` -- the accessor the driver has always used --
    six of them broke at once. A fake that re-declares a contract instead of
    deriving it drifts silently; ``test_the_fake_matches_the_real_context``
    below is what keeps this one honest.
    """

    @property
    def workspace(self):
        return Path(self.stand.path)


def _fake_ctx(**kw):
    kw.setdefault("stand", SimpleNamespace(path="/tmp/x"))
    kw.setdefault("issue_number", 7)
    return _FakeCtx(**kw)


def test_the_fake_matches_the_real_context():
    """The real RunContext must still derive workspace the way the fake does."""
    from performer.workflows.implementer.driver import RunContext

    assert isinstance(RunContext.workspace, property)
    stub = SimpleNamespace(stand=SimpleNamespace(path="/tmp/x"))
    assert RunContext.workspace.fget(stub) == _fake_ctx().workspace == Path("/tmp/x")


@pytest.mark.asyncio
async def test_a_failed_milestone_commits_and_pushes():
    """The whole point: the next container clones the same deterministic branch
    and finds the file, instead of starting from nothing."""
    from performer.workflows.implementer import salvage as sal

    committed: dict = {}

    async def fake_commit(workspace, paths, message):
        committed["paths"] = list(paths)
        committed["message"] = message
        return "newsha"

    async def fake_changed(workspace, since):
        return {"spec/a_spec.rb": "added"}

    push = AsyncMock()
    ctx = _fake_ctx(push=push)
    ok = await sal.salvage_failed_work(
        ctx, since_sha="oldsha", reason="red was not observed",
        _commit_paths=fake_commit, _changed_paths_since=fake_changed,
    )
    assert ok is True
    assert committed["paths"] == ["spec/a_spec.rb"]
    assert committed["message"].startswith(SALVAGE_PREFIX)
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_nothing_to_salvage_does_not_push():
    """An empty push moves nothing and costs a network round trip."""
    from performer.workflows.implementer import salvage as sal

    push = AsyncMock()
    ctx = _fake_ctx(push=push)
    ok = await sal.salvage_failed_work(
        ctx, since_sha="oldsha", reason="r",
        _commit_paths=AsyncMock(return_value=None),
        _changed_paths_since=AsyncMock(return_value={}),
    )
    assert ok is False
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_push_does_not_destroy_the_run():
    """Salvage is best effort. It runs on a path that is ALREADY reporting
    failure, so raising here would replace an honest partial_progress with a
    crash and lose the reason the operator needs."""
    from performer.workflows.implementer import salvage as sal

    ctx = _fake_ctx(push=AsyncMock(side_effect=RuntimeError("remote rejected")))
    ok = await sal.salvage_failed_work(
        ctx, since_sha="oldsha", reason="r",
        _commit_paths=AsyncMock(return_value="sha"),
        _changed_paths_since=AsyncMock(return_value={"spec/a.rb": "added"}),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_a_failed_commit_does_not_push():
    from performer.workflows.implementer import salvage as sal

    push = AsyncMock()
    ctx = _fake_ctx(push=push)
    ok = await sal.salvage_failed_work(
        ctx, since_sha="oldsha", reason="r",
        _commit_paths=AsyncMock(side_effect=RuntimeError("nothing staged")),
        _changed_paths_since=AsyncMock(return_value={"spec/a.rb": "added"}),
    )
    assert ok is False
    push.assert_not_awaited()
