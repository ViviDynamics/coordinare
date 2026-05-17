"""GitHub statusCheckRollup I/O for the closer PR-checks gate (spec 064).

This module is the *only* network surface for the gate. Decision logic lives
in `pr_checks_policy` and is pure so it can be unit-tested without mocks.
"""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


CheckStatus = Literal["queued", "in_progress", "completed"]
CheckConclusion = Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
]


class CheckEntry(BaseModel):
    """One leg of a PR's statusCheckRollup (a check-run or commit-status)."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    conclusion: CheckConclusion | None = None
    is_required: bool = False
    details_url: str | None = None


class CheckRollup(BaseModel):
    """Aggregate snapshot of a PR HEAD's required + non-required checks."""

    model_config = ConfigDict(frozen=True)

    pr_number: int
    head_sha: str
    head_pushed_at: datetime
    branch_protection_readable: bool
    checks: list[CheckEntry]
    at_context_cap: bool = False


# --- GraphQL query (kept in-module so tests don't need to read the contracts file) ---

_ROLLUP_QUERY = """
query PrCheckRollup($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      number
      baseRefName
      headRefOid
      commits(last: 1) {
        nodes {
          commit {
            oid
            pushedDate
            statusCheckRollup {
              state
              # NOTE: GitHub caps `first` at 100. PRs with >100 contexts will be
              # silently truncated — if this becomes a real constraint, add a
              # paginated follow-up using `pageInfo.endCursor` + `after:`.
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
          }
        }
      }
    }
    branchProtectionRules(first: 50) {
      nodes {
        pattern
        requiredStatusChecks {
          context
        }
      }
    }
  }
}
"""


# GitHub StatusContext state → CheckRun-style mapping so downstream policy can
# work off a single conclusion enum.
_STATUS_STATE_TO_CONCLUSION: dict[str, CheckConclusion | None] = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "ERROR": "failure",
    "EXPECTED": None,  # pending
    "PENDING": None,
}


def _normalize_conclusion(raw: str | None) -> CheckConclusion | None:
    if raw is None:
        return None
    lowered = raw.lower()
    valid = {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
    if lowered in valid:
        return lowered  # type: ignore[return-value]
    return None


def _normalize_status(raw: str | None) -> CheckStatus:
    if raw is None:
        return "queued"
    lowered = raw.lower()
    if lowered in {"queued", "in_progress", "completed"}:
        return lowered  # type: ignore[return-value]
    if lowered in {"requested", "waiting", "pending"}:
        return "queued"
    return "queued"


def _branch_matches(pattern: str, branch: str) -> bool:
    """Minimal glob match for branch-protection rule patterns.

    Supports `*` wildcards.  Anything else is treated as a literal match.
    """
    if pattern == branch:
        return True
    if "*" not in pattern:
        return False
    return fnmatch.fnmatchcase(branch, pattern)


def parse_rollup(data: dict[str, Any], pr_number: int) -> CheckRollup:
    """Parse a GraphQL response into a CheckRollup.

    Separated from the network call so tests can feed canned payloads.
    """
    repo = (data or {}).get("repository") or {}
    pr = repo.get("pullRequest") or {}
    commits = ((pr.get("commits") or {}).get("nodes") or [])
    if not commits:
        msg = f"PR #{pr_number}: no HEAD commit returned"
        raise ValueError(msg)
    commit = (commits[0] or {}).get("commit") or {}
    head_sha = commit.get("oid") or pr.get("headRefOid") or ""
    pushed_raw = commit.get("pushedDate")
    if not pushed_raw:
        msg = f"PR #{pr_number}: HEAD commit missing pushedDate"
        raise ValueError(msg)
    head_pushed_at = datetime.fromisoformat(pushed_raw)
    # GitHub always returns a UTC offset (`Z`/`+00:00`), but defensively
    # promote naive datetimes to UTC so downstream `now - head_pushed_at`
    # arithmetic in `pr_checks_policy.decide()` never raises TypeError.
    if head_pushed_at.tzinfo is None:
        head_pushed_at = head_pushed_at.replace(tzinfo=UTC)

    base_ref = pr.get("baseRefName") or ""

    # branchProtectionRules may be missing/None if token lacks admin:read.
    bpr_block = repo.get("branchProtectionRules")
    branch_protection_readable = bpr_block is not None
    required_names: set[str] = set()
    if bpr_block:
        for rule in (bpr_block.get("nodes") or []):
            if not rule:
                continue
            pattern = rule.get("pattern") or ""
            if not _branch_matches(pattern, base_ref):
                continue
            for rsc in (rule.get("requiredStatusChecks") or []):
                ctx = (rsc or {}).get("context")
                if ctx:
                    required_names.add(ctx)

    rollup_block = (commit.get("statusCheckRollup") or {})
    context_nodes = ((rollup_block.get("contexts") or {}).get("nodes") or [])
    # GraphQL `first: 100` cap — beyond this we may be silently missing a
    # required check. The caller is responsible for surfacing this (deduped
    # per HEAD) so monorepos with >100 contexts get a paginated follow-up
    # rather than a false FORWARD on every poll.
    at_context_cap = len(context_nodes) >= 100
    entries: list[CheckEntry] = []
    for node in context_nodes:
        if not node:
            continue
        typename = node.get("__typename")
        if typename == "CheckRun":
            name = node.get("name") or ""
            entry = CheckEntry(
                name=name,
                status=_normalize_status(node.get("status")),
                conclusion=_normalize_conclusion(node.get("conclusion")),
                is_required=branch_protection_readable and name in required_names,
                details_url=node.get("detailsUrl"),
            )
        elif typename == "StatusContext":
            name = node.get("context") or ""
            state = (node.get("state") or "").upper()
            conclusion = _STATUS_STATE_TO_CONCLUSION.get(state)
            status: CheckStatus = "completed" if conclusion is not None else "in_progress"
            entry = CheckEntry(
                name=name,
                status=status,
                conclusion=conclusion,
                is_required=branch_protection_readable and name in required_names,
                details_url=node.get("targetUrl"),
            )
        else:
            continue
        entries.append(entry)

    return CheckRollup(
        pr_number=pr_number,
        head_sha=head_sha,
        head_pushed_at=head_pushed_at,
        branch_protection_readable=branch_protection_readable,
        checks=entries,
        at_context_cap=at_context_cap,
    )


class PrChecksService:
    """Thin wrapper around `GitHubService._execute` for the rollup query."""

    def __init__(self, github_service: Any, owner: str, repo: str) -> None:
        self._gh = github_service
        self._owner = owner
        self._repo = repo

    async def get_pr_check_rollup(self, pr_number: int) -> CheckRollup:
        variables = {"owner": self._owner, "repo": self._repo, "pr": pr_number}
        try:
            data = await self._gh._execute(_ROLLUP_QUERY, variables)
        except Exception:
            logger.exception("pr_checks.rollup_query_failed", pr=pr_number)
            raise
        try:
            return parse_rollup(data, pr_number)
        except ValueError:
            # Malformed payload (missing pushedDate, no HEAD commit, etc.). The
            # caller's fail-open path will swallow this — surface it loudly here
            # so it's visible in logs/metrics rather than dissolving into a
            # WARNING about "fail_open_on_error".
            logger.error(
                "pr_checks.rollup_malformed",
                pr=pr_number,
                owner=self._owner,
                repo=self._repo,
            )
            raise
