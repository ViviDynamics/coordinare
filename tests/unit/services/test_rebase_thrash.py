"""096 (T013/FR-007) — anti-thrash guard for the proactive rebase trigger."""
from __future__ import annotations

from coordinare.models.rebase import RebaseOutcome
from coordinare.services.rebase import should_attempt_rebase

_MAIN = "a" * 40
_HEAD = "b" * 40


def test_no_prior_attempt_allows_rebase() -> None:
    assert should_attempt_rebase({}, _MAIN, _HEAD) is True
    assert should_attempt_rebase({"last_rebase_attempt": None}, _MAIN, _HEAD) is True


def test_blocked_same_main_and_head_is_skipped() -> None:
    """An unresolvable conflict against the same (main, head) is not re-attempted."""
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.BLOCKED.value}}
    assert should_attempt_rebase(sess, _MAIN, _HEAD) is False


def test_blocked_but_head_changed_is_re_attempted() -> None:
    """The performer pushed new work → re-attempt."""
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.BLOCKED.value}}
    assert should_attempt_rebase(sess, _MAIN, "c" * 40) is True


def test_blocked_but_main_changed_is_re_attempted() -> None:
    """Main moved → the conflict may now resolve → re-attempt."""
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.BLOCKED.value}}
    assert should_attempt_rebase(sess, "d" * 40, _HEAD) is True


def test_failed_same_main_and_head_is_skipped() -> None:
    sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD,
                                    "outcome": RebaseOutcome.FAILED.value}}
    assert should_attempt_rebase(sess, _MAIN, _HEAD) is False


def test_progressing_outcome_does_not_block() -> None:
    """A prior CLEAN/PERFORMER_RESOLVED/SKIPPED never thrash-guards."""
    for ok in (RebaseOutcome.CLEAN, RebaseOutcome.PERFORMER_RESOLVED, RebaseOutcome.SKIPPED):
        sess = {"last_rebase_attempt": {"main_sha": _MAIN, "head_sha": _HEAD, "outcome": ok.value}}
        assert should_attempt_rebase(sess, _MAIN, _HEAD) is True
