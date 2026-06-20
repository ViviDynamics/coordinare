"""097 (T002/T003) — pure pre-dispatch rebase decision classifier.

`classify_pre_dispatch` maps a branch's mergeability + anti-thrash marker to one
of {proceed, rebase, defer, blocked_thrash}, with NO I/O. It delegates the
thrash check to spec-096's `should_attempt_rebase`.
"""
from __future__ import annotations

import pytest

from coordinare.models.rebase import RebaseOutcome
from coordinare.services.rebase import classify_pre_dispatch

_MAIN = "a" * 40
_HEAD = "b" * 40


@pytest.mark.parametrize("raw,mss", [("UNKNOWN", ""), ("", "CLEAN"), ("UNKNOWN", "BEHIND")])
def test_unknown_mergeability_defers(raw, mss) -> None:
    assert classify_pre_dispatch(raw, mss, _HEAD, {}, _MAIN) == "defer"


def test_empty_head_defers() -> None:
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", "", {}, _MAIN) == "defer"


@pytest.mark.parametrize("raw,mss", [("MERGEABLE", "CLEAN"), ("MERGEABLE", "BLOCKED")])
def test_current_branch_proceeds(raw, mss) -> None:
    # MERGEABLE and not BEHIND → current → dispatch as today
    assert classify_pre_dispatch(raw, mss, _HEAD, {}, _MAIN) == "proceed"


def test_conflicting_no_marker_rebases() -> None:
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", _HEAD, {}, _MAIN) == "rebase"


def test_behind_no_marker_rebases() -> None:
    assert classify_pre_dispatch("MERGEABLE", "BEHIND", _HEAD, {}, _MAIN) == "rebase"


def test_conflicting_blocked_same_main_head_is_thrash_guarded() -> None:
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.BLOCKED.value}}
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", _HEAD, sess, _MAIN) == "blocked_thrash"


def test_conflicting_blocked_but_head_changed_rebases_again() -> None:
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.BLOCKED.value}}
    assert classify_pre_dispatch("CONFLICTING", "DIRTY", "c" * 40, sess, _MAIN) == "rebase"
