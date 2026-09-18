"""Shared stage, marker, and column constants for the performer monitor (435)."""

from __future__ import annotations

# 358: how many performer events the session keeps for the dashboard.
MAX_PERFORMER_EVENTS = 100


#: 380: how much wider the dispatch-anchored backstop is than the stall
#: ceiling. A performer looping on a failing command keeps producing, so the
#: stall clock resets forever; this bounds the run regardless. Wide enough that
#: it never fires on legitimate long work, which is why the stall ceiling
#: remains the one that normally acts.
ABSOLUTE_CEILING_MULTIPLIER = 4


TERMINAL_SUCCESS_STATES: frozenset[str] = frozenset({
    "pr_opened",
    "plan_committed",
    "approved",
    "security_passed",
    "qa_passed",
    "docs_committed",
    "assessment_complete",
    # 412: explicit advance-with-note verdicts. The reviewer parsed an empty
    # diff; the security stage found no statically scannable source. They end
    # their stage and are recorded, but are deliberately NOT expected markers
    # for a stage's primary pass -- re-running the stage on the same head is
    # what skips below.
    "nothing_to_review",
    "nothing_to_scan",
    "not_applicable",
})
# 125: verdict stages whose passing terminal marker is recorded as a
# stage-verdict slot ({head_sha, verdict, recorded_at}) so dispatch can skip
# re-running a stage whose verdict already covers the current PR head.
# implementing/assessing are deliberately absent (FR-004: never skipped, never
# recorded). The marker↔stage map guards against a cross-wired response (e.g.
# a stray "approved" while the stage is qa) minting a verdict for the wrong
# stage.
# 412: values are frozensets. A stage's accepted markers include the
# advance-with-note verdicts (412) beside the primary pass, so a recorded
# ``nothing_to_scan`` skips re-scanning the same head instead of re-running
# the whole stage it just completed.
VERDICT_STAGES: frozenset[str] = frozenset(
    {"reviewing", "security", "qa", "documenting", "closing_review"},
)
EXPECTED_STAGE_MARKER: dict[str, frozenset[str]] = {
    "reviewing": frozenset({"approved", "nothing_to_review"}),
    "security": frozenset({"security_passed", "nothing_to_scan", "not_applicable"}),
    "qa": frozenset({"qa_passed"}),
    "documenting": frozenset({"docs_committed"}),
    # 412 round 8: the reviewer workflow can answer an empty-diff closing
    # review with nothing_to_review -- without it in the expected set the
    # verdict advances but is never recorded, so the same head re-dispatches.
    "closing_review": frozenset({"approved", "nothing_to_review"}),
}

# 072: performer stages for which a trailing partial_progress sentinel is
# honored. Architecting / assessing / closing-review / env_bootstrap are
# short single-turn roles where checkpointing does not apply.
#
# IMPORTANT: must stay in sync with ``SENTINEL_ROLES`` in
# ``agent/performer/src/performer/main.py``. The set is duplicated across
# the two processes (coordinare + performer container) because there is no
# shared library between them; if you add a role here, add it there too.
SENTINEL_STAGES: frozenset[str] = frozenset(
    {"implementing", "reviewing", "security", "qa", "documenting"},
)

# 072: per-role zero-progress guardrail applies to non-implementer
# review-style stages. The implementing stage uses the simpler
# single-signal (head-delta only) guardrail from 070 — commits ARE the
# progress signal there.
ZERO_PROGRESS_REVIEW_STAGES: frozenset[str] = frozenset(
    {"reviewing", "security", "qa", "documenting"},
)


# 072 FR-072-10: terminal markers that may carry a settled head_after worth
# recording on ``head_at_last_turn``. Mirrors the slot-release allowlist so
# new non-terminal markers cannot accidentally trip the audit-trail write.
_TERMINAL_MARKERS_FOR_HEAD: frozenset[str] = TERMINAL_SUCCESS_STATES | frozenset({
    "changes_requested", "security_failed", "qa_failed", "qa_env_blocked",
    "error", "blocked", "session_expired", "token_limit",
    "partial_progress",
})

# 072 FR-072-5: per-role "resume" relay-feedback text used when the
# zero-progress guardrail trips. Lifted to module scope so we don't rebuild
# the dict on every monitor pass.
_ROLE_RESUME_DIRECTIVES: dict[str, str] = {
    "reviewing": (
        "Resume your review of the PR diff; post comments on specific "
        "changes or emit `partial_progress` if you need to checkpoint."
    ),
    "security": (
        "Resume your security audit; surface findings as PR comments or "
        "emit `partial_progress` if you need to checkpoint."
    ),
    "qa": (
        "Resume your QA pass; post test results as PR comments or emit "
        "`partial_progress` if you need to checkpoint."
    ),
    "documenting": (
        "Resume your documentation pass; commit doc changes or emit "
        "`partial_progress` if you need to checkpoint."
    ),
}
_DEFAULT_RESUME_DIRECTIVE = "Resume your work on this card."


# 032: Phase → expected board column mapping for reconciliation
PHASE_TO_EXPECTED_COLUMN: dict[str, str] = {
    "monitoring_agent": "IN_PROGRESS",
    "monitoring_performer": "IN_PROGRESS",
    "monitoring_pr": "IN_REVIEW",
    "merging": "IN_REVIEW",
}

