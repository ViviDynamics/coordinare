"""GitHub statusCheckRollup I/O for the closer PR-checks gate (spec 064).

This module is the *only* network surface for the gate. Decision logic lives
in `pr_checks_policy` and is pure so it can be unit-tested without mocks.
"""

from __future__ import annotations

import fnmatch
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

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
    # Failure text lifted from a CheckRun's output{} block (spec-090 F1). Feeds
    # the L2 classifier's reason-sensitive signature and the L3 repair mandate.
    # Always None for legacy StatusContext rows (commit statuses have no output).
    title: str | None = None
    summary: str | None = None


class CheckRollup(BaseModel):
    """Aggregate snapshot of a PR HEAD's required + non-required checks."""

    model_config = ConfigDict(frozen=True)

    pr_number: int
    head_sha: str
    # Optional because a *base*-origin rollup (spec-090 F2) has no meaningful
    # "head push" timestamp — the base is a long-lived branch, not a PR HEAD, so
    # the push-age timeout in `pr_checks_policy.decide()` must not apply to it.
    # The head path always sets a real datetime, so it stays byte-identical.
    head_pushed_at: datetime | None
    branch_protection_readable: bool
    checks: list[CheckEntry]
    at_context_cap: bool = False
    base_ref: str = ""
    # Where this rollup came from. "head" (default) is the PR HEAD rollup the
    # closer gate has always consumed; "base" is the base-branch baseline rollup
    # introduced for the L1 prevention gate / L2 classifier (spec-090).
    rollup_origin: Literal["head", "base"] = "head"


# --- GraphQL query (kept in-module so tests don't need to read the contracts file) ---

# Shared statusCheckRollup selection. Reused by the PR-HEAD query and the
# base-branch query (spec-090 F2) so the CheckRun output{} block (spec-090 F1)
# is defined in exactly one place — the head query is whitespace-identical to
# before (GraphQL ignores whitespace; tests parse canned payloads, not strings).
_STATUS_CHECK_ROLLUP = """
            statusCheckRollup {
              state
              # NOTE: GitHub caps `first` at 100. A head/branch with >100 contexts
              # will be silently truncated — if this becomes a real constraint, add
              # a paginated follow-up using `pageInfo.endCursor` + `after:`.
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                    output {
                      title
                      summary
                    }
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
"""

_ROLLUP_CORE = (
    "\n"
    "      number\n"
    "      baseRefName\n"
    "      headRefOid\n"
    "      commits(last: 1) {\n"
    "        nodes {\n"
    "          commit {\n"
    "            oid\n"
    "            pushedDate\n"
    "            committedDate\n"
    + _STATUS_CHECK_ROLLUP +
    "          }\n"
    "        }\n"
    "      }\n"
)

_ROLLUP_QUERY = (
    "query PrCheckRollup($owner: String!, $repo: String!, $pr: Int!) {\n"
    "  repository(owner: $owner, name: $repo) {\n"
    "    pullRequest(number: $pr) {\n"
    + _ROLLUP_CORE +
    "    }\n"
    "    branchProtectionRules(first: 50) {\n"
    "      nodes {\n"
    "        pattern\n"
    "        requiredStatusChecks {\n"
    "          context\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# Fallback query for tokens that lack admin:read (cannot see branchProtectionRules).
# We still get the rollup; parse_rollup will set branch_protection_readable=False
# and the policy will fall back to `treat_unknown_required_as`.
_ROLLUP_QUERY_NO_BPR = (
    "query PrCheckRollupNoBpr($owner: String!, $repo: String!, $pr: Int!) {\n"
    "  repository(owner: $owner, name: $repo) {\n"
    "    pullRequest(number: $pr) {\n"
    + _ROLLUP_CORE +
    "    }\n"
    "  }\n"
    "}\n"
)

# Base-branch rollup (spec-090 F2). Fetches the baseline check rollup off a
# branch ref (Ref → target → Commit) so the L1 prevention gate can read the base
# branch's REQUIRED-check health and the L2 classifier can diff against it.
_BASE_ROLLUP_QUERY = (
    "query BaseCheckRollup($owner: String!, $repo: String!, $qualifiedName: String!) {\n"
    "  repository(owner: $owner, name: $repo) {\n"
    "    ref(qualifiedName: $qualifiedName) {\n"
    "      target {\n"
    "        ... on Commit {\n"
    "          oid\n"
    "          pushedDate\n"
    "          committedDate\n"
    + _STATUS_CHECK_ROLLUP +
    "        }\n"
    "      }\n"
    "    }\n"
    "    branchProtectionRules(first: 50) {\n"
    "      nodes {\n"
    "        pattern\n"
    "        requiredStatusChecks {\n"
    "          context\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# Base-branch fallback for tokens lacking admin:read (no branchProtectionRules).
_BASE_ROLLUP_QUERY_NO_BPR = (
    "query BaseCheckRollupNoBpr($owner: String!, $repo: String!, $qualifiedName: String!) {\n"
    "  repository(owner: $owner, name: $repo) {\n"
    "    ref(qualifiedName: $qualifiedName) {\n"
    "      target {\n"
    "        ... on Commit {\n"
    "          oid\n"
    "          pushedDate\n"
    "          committedDate\n"
    + _STATUS_CHECK_ROLLUP +
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)


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


def _resolve_required_names(
    repo: dict[str, Any], branch_ref: str
) -> tuple[bool, set[str]]:
    """Resolve the set of required check names for a branch from branchProtectionRules.

    Returns ``(branch_protection_readable, required_names)``. When the token lacks
    admin:read the branchProtectionRules block is missing/None → readable is False
    and the set is empty (the caller then falls back to `treat_unknown_required_as`).
    Shared by the PR-HEAD and base-branch parsers so a base rollup resolves the
    base branch's OWN required set (spec-090 F2).
    """
    bpr_block = repo.get("branchProtectionRules")
    branch_protection_readable = bpr_block is not None
    required_names: set[str] = set()
    if bpr_block:
        for rule in (bpr_block.get("nodes") or []):
            if not rule:
                continue
            pattern = rule.get("pattern") or ""
            if not _branch_matches(pattern, branch_ref):
                continue
            for rsc in (rule.get("requiredStatusChecks") or []):
                ctx = (rsc or {}).get("context")
                if ctx:
                    required_names.add(ctx)
    return branch_protection_readable, required_names


def _parse_context_nodes(
    context_nodes: list[dict[str, Any]],
    *,
    branch_protection_readable: bool,
    required_names: set[str],
) -> list[CheckEntry]:
    """Parse statusCheckRollup context nodes into CheckEntry rows.

    Shared by `parse_rollup` (PR HEAD) and `parse_base_rollup` (base branch) so the
    CheckRun/StatusContext handling — including the F1 ``output{title,summary}``
    lift — lives in exactly one place (spec-090 F2).
    """
    entries: list[CheckEntry] = []
    for node in context_nodes:
        if not node:
            continue
        typename = node.get("__typename")
        if typename == "CheckRun":
            name = node.get("name") or ""
            output = node.get("output") or {}
            entry = CheckEntry(
                name=name,
                status=_normalize_status(node.get("status")),
                conclusion=_normalize_conclusion(node.get("conclusion")),
                is_required=branch_protection_readable and name in required_names,
                details_url=node.get("detailsUrl"),
                title=output.get("title"),
                summary=output.get("summary"),
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
    return entries


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
    # GitHub returns pushedDate: null for commits created via API/web edits
    # (e.g. app-authored commits, squash-merges-as-amends, signed commits via
    # GitHub UI). committedDate is always present and is an acceptable proxy
    # for "when this HEAD entered the PR" — strictly older than pushedDate,
    # so it only widens the gate's timeout window (fail-safe direction).
    pushed_raw = commit.get("pushedDate") or commit.get("committedDate")
    if not pushed_raw:
        msg = f"PR #{pr_number}: HEAD commit missing pushedDate and committedDate"
        raise ValueError(msg)
    head_pushed_at = datetime.fromisoformat(pushed_raw)
    # GitHub always returns a UTC offset (`Z`/`+00:00`), but defensively
    # promote naive datetimes to UTC so downstream `now - head_pushed_at`
    # arithmetic in `pr_checks_policy.decide()` never raises TypeError.
    if head_pushed_at.tzinfo is None:
        head_pushed_at = head_pushed_at.replace(tzinfo=UTC)

    base_ref = pr.get("baseRefName") or ""

    # branchProtectionRules may be missing/None if token lacks admin:read.
    branch_protection_readable, required_names = _resolve_required_names(repo, base_ref)

    rollup_block = (commit.get("statusCheckRollup") or {})
    context_nodes = ((rollup_block.get("contexts") or {}).get("nodes") or [])
    # GraphQL `first: 100` cap — beyond this we may be silently missing a
    # required check. The caller is responsible for surfacing this (deduped
    # per HEAD) so monorepos with >100 contexts get a paginated follow-up
    # rather than a false FORWARD on every poll.
    at_context_cap = len(context_nodes) >= 100
    entries = _parse_context_nodes(
        context_nodes,
        branch_protection_readable=branch_protection_readable,
        required_names=required_names,
    )

    return CheckRollup(
        pr_number=pr_number,
        head_sha=head_sha,
        head_pushed_at=head_pushed_at,
        branch_protection_readable=branch_protection_readable,
        checks=entries,
        at_context_cap=at_context_cap,
        base_ref=base_ref,
    )


def parse_base_rollup(data: dict[str, Any], *, base_ref: str) -> CheckRollup:
    """Parse a base-branch GraphQL response (Ref→target→Commit) into a CheckRollup.

    Stamps ``rollup_origin="base"`` with a neutral ``pr_number=0`` and
    ``head_pushed_at=None`` — the base is a long-lived branch, not a PR HEAD, so the
    push-age timeout in `pr_checks_policy.decide()` must not apply to it. Resolves the
    base branch's OWN required set from branch protection so the L1 prevention gate /
    L2 classifier reason about the base's required-check health (spec-090 F2).

    Raises ValueError when the ref has no target commit (deleted/ambiguous ref) so the
    service's fetch wrapper can fail-safe to None (FR-005).
    """
    repo = (data or {}).get("repository") or {}
    ref = repo.get("ref") or {}
    target = ref.get("target") or {}
    if not target:
        msg = f"base ref {base_ref!r}: no target commit returned"
        raise ValueError(msg)
    head_sha = target.get("oid") or ""

    branch_protection_readable, required_names = _resolve_required_names(repo, base_ref)

    rollup_block = (target.get("statusCheckRollup") or {})
    context_nodes = ((rollup_block.get("contexts") or {}).get("nodes") or [])
    at_context_cap = len(context_nodes) >= 100
    entries = _parse_context_nodes(
        context_nodes,
        branch_protection_readable=branch_protection_readable,
        required_names=required_names,
    )

    return CheckRollup(
        pr_number=0,
        head_sha=head_sha,
        head_pushed_at=None,
        branch_protection_readable=branch_protection_readable,
        checks=entries,
        at_context_cap=at_context_cap,
        base_ref=base_ref,
        rollup_origin="base",
    )


class _BaselineFetchFailureTracker:
    """Per-repo sliding-window counter for base-branch rollup fetch failures.

    Emits a single structlog *error* ``baseline_fetch_degraded`` once ≥5 fetch
    failures land within a 1-hour window (FR-027), so a persistently unreadable base
    surfaces loudly instead of the L1 gate silently fail-opening on every poll. The
    monotonic clock is injectable so the window behaviour is deterministic in tests.
    """

    _WINDOW_SECONDS: float = 3600.0
    _THRESHOLD: int = 5

    def __init__(
        self, owner: str, repo: str, *, clock: Callable[[], float] | None = None
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._clock = clock or time.monotonic
        self._failures: deque[float] = deque()

    def record_failure(self) -> bool:
        """Record one fetch failure; age out failures older than the window, then
        return True (emitting the degraded signal) when the in-window count reaches
        the threshold."""
        now = self._clock()
        self._failures.append(now)
        cutoff = now - self._WINDOW_SECONDS
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        degraded = len(self._failures) >= self._THRESHOLD
        if degraded:
            logger.error(
                "baseline_fetch_degraded",
                owner=self._owner,
                repo=self._repo,
                failures=len(self._failures),
                window_seconds=self._WINDOW_SECONDS,
            )
        return degraded


class PrChecksService:
    """Thin wrapper around `GitHubService._execute` for the rollup query."""

    # Re-probe the full query (with branchProtectionRules) periodically so a
    # token rotated to include admin:read is picked up without a restart.
    _BPR_REPROBE_AFTER_SECONDS: float = 3600.0

    def __init__(self, github_service: Any, owner: str, repo: str) -> None:
        self._gh = github_service
        self._owner = owner
        self._repo = repo
        # Once we see a FORBIDDEN on branchProtectionRules for this repo, stop
        # asking for it — every subsequent poll would log the same traceback.
        self._bpr_forbidden: bool = False
        self._bpr_forbidden_at: float = 0.0
        # FR-027: surface a persistently unreadable base branch (spec-090 F2).
        self._baseline_failures = _BaselineFetchFailureTracker(owner, repo)

    async def get_pr_check_rollup(self, pr_number: int) -> CheckRollup:
        variables = {"owner": self._owner, "repo": self._repo, "pr": pr_number}
        if self._bpr_forbidden and (
            time.monotonic() - self._bpr_forbidden_at
            >= self._BPR_REPROBE_AFTER_SECONDS
        ):
            self._bpr_forbidden = False
        query = _ROLLUP_QUERY_NO_BPR if self._bpr_forbidden else _ROLLUP_QUERY
        try:
            data = await self._gh._execute(query, variables)
        except Exception as exc:
            # Token lacks admin:read for branch protection. Retry once without
            # the branchProtectionRules block; future polls skip it entirely.
            msg = str(exc)
            if (
                not self._bpr_forbidden
                and "FORBIDDEN" in msg
                and "branchProtectionRules" in msg
            ):
                self._bpr_forbidden = True
                self._bpr_forbidden_at = time.monotonic()
                logger.warning(
                    "pr_checks.branch_protection_forbidden",
                    pr=pr_number,
                    owner=self._owner,
                    repo=self._repo,
                )
                try:
                    data = await self._gh._execute(_ROLLUP_QUERY_NO_BPR, variables)
                except Exception:
                    logger.exception("pr_checks.rollup_query_failed", pr=pr_number)
                    raise
            else:
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

    async def get_base_branch_check_rollup(
        self, base_ref: str
    ) -> CheckRollup | None:
        """Fetch the base branch's check rollup; fail-safe to None (FR-005).

        Never raises: any fetch error / timeout / malformed payload returns None so
        the L1 prevention gate fails *open* (never hard-blocking a merge on an
        unreadable base). Persistent failures feed the FR-027 degraded tracker. An
        empty/blank base ref is a degenerate input — return None without a fetch (and
        without recording it as a transient failure).
        """
        if not base_ref:
            return None
        qualified = (
            base_ref if base_ref.startswith("refs/") else f"refs/heads/{base_ref}"
        )
        variables = {
            "owner": self._owner,
            "repo": self._repo,
            "qualifiedName": qualified,
        }
        # Reuse the repo-level admin:read probe state so a base fetch never asks for
        # branchProtectionRules once it's known-forbidden (mirrors the HEAD path).
        if self._bpr_forbidden and (
            time.monotonic() - self._bpr_forbidden_at
            >= self._BPR_REPROBE_AFTER_SECONDS
        ):
            self._bpr_forbidden = False
        query = _BASE_ROLLUP_QUERY_NO_BPR if self._bpr_forbidden else _BASE_ROLLUP_QUERY
        try:
            data = await self._gh._execute(query, variables)
            return parse_base_rollup(data, base_ref=base_ref)
        except Exception:
            logger.warning(
                "pr_checks.base_rollup_fetch_failed",
                owner=self._owner,
                repo=self._repo,
                base_ref=base_ref,
            )
            self._baseline_failures.record_failure()
            return None
