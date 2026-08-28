"""Spec 134 — in-process fake GitHub service for the board-simulation benchmark.

``FakeGitHubService`` satisfies ``GitHubServiceProtocol`` (``graph/state.py``) and
stands in for the real ``GitHubService`` so the benchmark can drive the *real*
coordinare daemon end-to-end without ever touching live GitHub. Only the GitHub
API is faked — git is a real local ``file://`` bare repo and CI is a real pytest
run against the PR head.

Design invariants (see specs/134-board-sim-benchmark/):

* **Plain mutable object** — a graph node writes ``_pr_checks_service_cache`` onto
  the service (monitor_performer), so this must not be slotted/frozen.
* **Never raises** — every method degrades to a safe default. A raise inside a
  graph node aborts ``daemon.start()`` instead of letting the loop terminate
  cleanly, so the fake must swallow and log, never propagate.
* **Faithful shapes** — reads return the exact dict shapes the real service
  returns (verified against services/github.py), so coordinare's decision logic
  behaves identically.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from coordinare.services.pr_checks_service import CheckEntry, CheckRollup

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

# Default CI command run against a PR-head checkout. Overridable per run so a
# fixture can express its own acceptance suite.
DEFAULT_TEST_COMMAND: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q")


class _PrChecksCache(dict[Any, Any]):
    """Cache stand-in the CI-gate node finds on ``github._pr_checks_service_cache``.

    monitor_performer does ``cache = getattr(github, "_pr_checks_service_cache",
    None) or {}`` and then only builds a real ``PrChecksService`` when the
    ``(owner, repo)`` key is *absent*. To force our fake CI service to always win
    regardless of how the node parses ``(owner, repo)``, this dict:

    * is always truthy (so the ``or {}`` never discards it), and
    * reports every key as present (so the node never overwrites with a real
      ``PrChecksService``), lazily minting our fake on first access.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        super().__init__()
        self._factory = factory

    def __bool__(self) -> bool:  # never falsy — survive `... or {}`
        return True

    def __contains__(self, key: object) -> bool:  # every key "present"
        return True

    def __getitem__(self, key: Any) -> Any:
        if not super().__contains__(key):
            super().__setitem__(key, self._factory())
        return super().__getitem__(key)


class _FakePrChecks:
    """Fake ``PrChecksService`` — builds a ``CheckRollup`` from a real pytest run.

    Mirrors the two entry points the gate uses: ``get_pr_check_rollup`` (PR HEAD)
    and ``get_base_branch_check_rollup`` (base baseline, fail-safe to None).
    """

    def __init__(self, fake: FakeGitHubService) -> None:
        self._fake = fake

    async def get_pr_check_rollup(self, pr_number: int) -> CheckRollup:
        return await self._fake._ci_rollup_for_number(pr_number)

    async def get_base_branch_check_rollup(self, base_ref: str) -> CheckRollup | None:
        # Base baseline is the spec-090 L1 prevention gate input; the real service
        # is documented fail-safe-to-None. We return None so the head rollup alone
        # governs the gate (head must be green) — see research.md D4.
        return None


class FakeGitHubService:
    """In-process GitHub API stand-in backed by a local bare git repo."""

    def __init__(
        self,
        *,
        bare_repo_path: str | Path,
        org: str = "bench-org",
        project_name: str = "bench-repo",
        human_reviewers: list[str] | None = None,
        approver: Callable[[dict[str, Any]], bool] | None = None,
        test_command: tuple[str, ...] = DEFAULT_TEST_COMMAND,
        work_dir: str | Path | None = None,
    ) -> None:
        self._bare_repo = Path(bare_repo_path)
        self._org = org
        self._project_name = project_name
        self.project_id = "PVT_fake"  # non-None so getattr(github, "project_id") reads succeed
        self.field_cache: dict[str, Any] = {}
        self._human_reviewers = [r.lower() for r in (human_reviewers or [])]
        self._comment_seq = 0  # monotonic issue-comment id (since_id watermarking)
        # Default approver never approves; the benchmark injects gates_green (spec-134 US3).
        self._approver: Callable[[dict[str, Any]], bool] = approver or (lambda _state: False)
        self._test_command = tuple(test_command)
        self._work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="bench-ci-"))
        self._work_dir.mkdir(parents=True, exist_ok=True)

        # State model (per run).
        self._cards: dict[str, dict[str, Any]] = {}
        self._prs: dict[str, dict[str, Any]] = {}
        self._ci_cache: dict[str, CheckRollup] = {}
        self._pr_counter = 0
        self.events: list[dict[str, Any]] = []

        # Pre-populate the CI cache seam so the node uses our fake PrChecksService.
        self._pr_checks_service_cache = _PrChecksCache(lambda: _FakePrChecks(self))

    # ---- properties / lifecycle / auth -----------------------------------

    @property
    def org(self) -> str:
        return self._org

    @property
    def project_name(self) -> str:
        return self._project_name

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        # Best-effort scratch cleanup; never raises.
        import shutil

        try:
            shutil.rmtree(self._work_dir, ignore_errors=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("fake_github.aclose_failed", error=str(exc))

    async def current_token(self) -> str:
        return "fake-token"

    async def _current_token(self) -> str:
        return "fake-token"

    # ---- harness setup helpers (not part of the Protocol) ----------------

    def seed_card(
        self,
        item_id: str,
        *,
        title: str,
        body: str,
        status: str = "TODO",
        issue_number: int,
        issue_node_id: str | None = None,
        issue_url: str | None = None,
        labels: list[str] | None = None,
        fixture_id: str | None = None,
    ) -> None:
        """Register a board card (a from-scratch work item) in the given column."""
        self._cards[item_id] = {
            "status": status,
            "title": title,
            "body": body,
            "issue_number": issue_number,
            "issue_node_id": issue_node_id or f"I_{item_id}",
            "content_node_id": issue_node_id or f"I_{item_id}",
            "issue_url": issue_url or f"https://fake/{self._org}/{self._project_name}/issues/{issue_number}",
            "labels": labels or [],
            "assignees": [],
            "pr_item_id": None,
            "fixture_id": fixture_id,
        }
        self._record("seed_card", item_id=item_id, title=title, status=status)

    def open_pr(
        self,
        *,
        issue_item_id: str,
        head_ref: str,
        base_ref: str = "main",
    ) -> str:
        """Create a PR for a card's head branch (simulates the performer's push+PR).

        Returns the PR node id. In a stubbed run the harness calls this; real-run
        auto-discovery from pushed branches is wired in the runner.
        """
        self._pr_counter += 1
        number = self._pr_counter
        pr_id = f"PR_{number}"
        url = f"https://fake/{self._org}/{self._project_name}/pull/{number}"
        self._prs[pr_id] = {
            "pr_id": pr_id,
            "pr_number": number,
            "url": url,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "issue_item_id": issue_item_id,
            "reviews": [],
            "comments": [],
            "merged": False,
            "merge_commit": None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if issue_item_id in self._cards:
            self._cards[issue_item_id]["pr_item_id"] = pr_id
        self._record("open_pr", pr_id=pr_id, head_ref=head_ref, issue_item_id=issue_item_id)
        return pr_id

    def _record(self, kind: str, **data: Any) -> None:
        self.events.append({"kind": kind, "at": datetime.now(UTC).isoformat(), **data})

    # ---- git plumbing (all best-effort; never raise) ---------------------

    async def _git(self, *args: str, cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=str(cwd or self._bare_repo),
                capture_output=True,
                text=True,
                timeout=60,
            )

        return await asyncio.to_thread(run)

    async def _rev(self, ref: str) -> str:
        try:
            r = await self._git("rev-parse", ref)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception as exc:
            logger.warning("fake_github.rev_parse_failed", ref=ref, error=str(exc))
            return ""

    def _pr_by_number(self, pr_number: int) -> dict[str, Any] | None:
        for pr in self._prs.values():
            if pr["pr_number"] == pr_number:
                return pr
        return None

    def _pr_by_url(self, pr_url: str) -> dict[str, Any] | None:
        for pr in self._prs.values():
            if pr["url"] == pr_url:
                return pr
        # tolerate a trailing-number parse
        tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return self._pr_by_number(int(tail))
        return None

    # ---- board / issues --------------------------------------------------

    async def poll_board(self) -> dict[str, Any]:
        snapshot: dict[str, list[str]] = {
            "BACKLOG": [], "TODO": [], "BLOCKED": [],
            "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": [],
        }
        titles: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        issue_numbers: dict[str, int] = {}
        issue_urls: dict[str, str] = {}
        item_labels: dict[str, list[str]] = {}
        item_assignees: dict[str, list[str]] = {}
        content_node_ids: dict[str, str] = {}
        pr_urls: dict[str, str] = {}
        for item_id, card in self._cards.items():
            snapshot.setdefault(card["status"], []).append(item_id)
            titles[item_id] = card["title"]
            descriptions[item_id] = card["body"]
            issue_numbers[item_id] = card["issue_number"]
            issue_urls[item_id] = card["issue_url"]
            item_labels[item_id] = list(card["labels"])
            item_assignees[item_id] = list(card["assignees"])
            content_node_ids[item_id] = card["content_node_id"]
            pr_item = card.get("pr_item_id")
            if pr_item and pr_item in self._prs:
                pr_urls[item_id] = self._prs[pr_item]["url"]
        return {
            "snapshot": snapshot,
            "titles": titles,
            "descriptions": descriptions,
            "issue_numbers": issue_numbers,
            "issue_urls": issue_urls,
            "item_labels": item_labels,
            "item_assignees": item_assignees,
            "content_node_ids": content_node_ids,
            "pr_urls": pr_urls,
        }

    async def move_card(self, item_id: str, status: str) -> None:
        if item_id in self._cards:
            self._cards[item_id]["status"] = status
            self._record("move_card", item_id=item_id, status=status)

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        for card in self._cards.values():
            if card["content_node_id"] == issue_id or card["issue_node_id"] == issue_id:
                return {
                    "id": issue_id,
                    "title": card["title"],
                    "body": card["body"],
                    "number": card["issue_number"],
                    "url": card["issue_url"],
                    "state": "OPEN",
                }
        return {}

    async def check_issue_state(self, repo: str, issue_number: int) -> str:
        for card in self._cards.values():
            if card["issue_number"] == issue_number:
                return "closed" if card["status"] == "DONE" else "open"
        return "open"

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict[str, Any]]:
        # 151 review fix: was `return []` while post_comment only recorded an event,
        # so comments were write-only — the security stage's dedup read saw an empty
        # conversation every cycle and re-posted the same advisory finding forever,
        # and findings never reached the artifact. Now both sides share pr["comments"].
        # Returns the COORDINARE-normalised shape (id/author/body) that the real
        # GitHubService.get_issue_comments emits; FakeGitHubServer serves the raw
        # GitHub wire shape to the performer from the same list.
        pr = self._pr_by_number(issue_number)
        if pr is None:
            return []
        out: list[dict[str, Any]] = []
        for c in pr["comments"]:
            cid = int(c.get("id", 0))
            if since_id is not None and cid <= since_id:
                continue
            author = c.get("user") or {}
            out.append({
                "id": cid,
                "author": str(author.get("login", "")) if isinstance(author, dict) else "",
                "body": str(c.get("body", "")),
            })
        return out

    async def list_open_issues(self, owner: str, repo: str, first: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for card in self._cards.values():
            if card["status"] != "DONE":
                out.append({
                    "id": card["issue_node_id"],
                    "number": card["issue_number"],
                    "title": card["title"],
                    "url": card["issue_url"],
                })
        return out[:first]

    async def link_to_project(self, content_id: str, status: str = "IN_REVIEW") -> str | None:
        return f"PVTI_{content_id}"

    # ---- PR / review -----------------------------------------------------

    async def find_pr_for_issue(self, issue_node_id: str) -> dict[str, str] | None:
        for card in self._cards.values():
            if card["issue_node_id"] != issue_node_id:
                continue
            pr_item = card.get("pr_item_id")
            if pr_item and pr_item in self._prs and not self._prs[pr_item]["merged"]:
                pr = self._prs[pr_item]
                return {"pr_node_id": pr["pr_id"], "pr_url": pr["url"]}
        return None

    async def count_closed_prs_for_issue(self, issue_node_id: str) -> int:
        return 0

    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]:
        ctx = await self.get_pr_review_context(pr_id)
        reviews: list[dict[str, Any]] = ctx["reviews"]
        return reviews

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        pr = self._prs.get(pr_id)
        if pr is None:
            return {"reviews": [], "review_threads": [], "head_oid": "", "review_decision": ""}
        await self._maybe_approve(pr)
        head_oid = await self._rev(pr["head_ref"])
        return {
            "reviews": list(pr["reviews"]),
            "review_threads": [],
            "head_oid": head_oid,
            "review_decision": self._review_decision(pr),
        }

    async def request_reviews(self, pr_id: str, reviewer_logins: list[str]) -> dict[str, Any]:
        self._record("request_reviews", pr_id=pr_id, reviewers=reviewer_logins)
        return {"requested": True, "reason": "re-requested", "count": len(reviewer_logins)}

    async def request_reviewers(self, owner: str, repo: str, pr_number: int, reviewers: list[str]) -> None:
        self._record("request_reviewers", pr_number=pr_number, reviewers=reviewers)

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        pr = self._prs.get(pr_id)
        if pr is None:
            return {"mergeable": False, "reason": "missing_pr"}
        await self._maybe_approve(pr)
        review_decision = self._review_decision(pr)
        mergeable_raw = "MERGEABLE" if not pr["merged"] else "UNKNOWN"
        approved = review_decision == "APPROVED"
        return {
            "mergeable": (mergeable_raw == "MERGEABLE") and approved,
            "mergeable_raw": mergeable_raw,
            "merge_state_status": "CLEAN" if approved else "BLOCKED",
            "review_decision": review_decision,
            "head_ref_oid": await self._rev(pr["head_ref"]),
            "head_ref_name": pr["head_ref"],
        }

    async def squash_merge(self, pr_id: str) -> dict[str, Any]:
        pr = self._prs.get(pr_id)
        if pr is None:
            return {"merged": False}
        oid = await self._do_local_merge(pr)
        if not oid:
            return {"merged": False, "reason": "merge_failed"}
        pr["merged"] = True
        pr["merge_commit"] = {"oid": oid, "messageHeadline": f"Squash merge {pr['head_ref']}"}
        self._record("squash_merge", pr_id=pr_id, merge_commit=oid)
        return {"merged": True, "id": pr_id, "merge_commit": pr["merge_commit"]}

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self._record("add_comment", subject_id=subject_id, body=body[:200])
        return {"id": f"IC_{len(self.events)}"}

    async def post_comment(
        self, issue_number: int, body: str, *, author: str = "coordinare-bot"
    ) -> dict[str, Any]:
        # Latent, swallowed call in monitor_performer (does NOT exist on the real
        # service). We record it so the branch is observable rather than lost.
        self._record("post_comment", issue_number=issue_number, body=body[:200])
        # 151 review fix: also persist it onto the PR so get_issue_comments (and the
        # performer's dedup read through FakeGitHubServer) can actually see it.
        # Stored in the GitHub wire shape; int ids so `since_id` watermarking works.
        pr = self._pr_by_number(issue_number)
        self._comment_seq += 1
        comment = {
            "id": self._comment_seq,
            "user": {"login": author},
            "body": body,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if pr is not None:
            pr["comments"].append(comment)
        return comment

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        pr = self._pr_by_number(pr_number)
        if pr is None:
            return {"files": [], "truncated": False}
        files = await self._changed_files(pr["base_ref"], pr["head_ref"])
        return {"files": [{"path": f} for f in files], "truncated": False}

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        pr = self._pr_by_url(pr_url)
        if pr is None:
            return ("", [])
        try:
            d = await self._git("diff", f"{pr['base_ref']}...{pr['head_ref']}")
            diff_text = d.stdout if d.returncode == 0 else ""
        except Exception as exc:
            logger.warning("fake_github.get_pr_diff_failed", pr_url=pr_url, error=str(exc))
            diff_text = ""
        files = await self._changed_files(pr["base_ref"], pr["head_ref"])
        return (diff_text, files)

    async def compare_changed_files(self, pr_url: str, base_sha: str, head_sha: str) -> list[str]:
        return await self._changed_files(base_sha, head_sha)

    async def _changed_files(self, base: str, head: str) -> list[str]:
        try:
            r = await self._git("diff", "--name-only", f"{base}...{head}")
            if r.returncode != 0:
                return []
            return [ln for ln in r.stdout.splitlines() if ln.strip()]
        except Exception as exc:
            logger.warning("fake_github.changed_files_failed", error=str(exc))
            return []

    # ---- CI / checks -----------------------------------------------------

    async def get_required_status_checks(self, owner: str, repo: str, default_branch: str) -> set[str] | None:
        return {"pytest"}

    async def fetch_failed_job_log(self, owner: str, repo: str, job_id: int, max_chars: int = 6000) -> str:
        return ""

    async def _ci_rollup_for_number(self, pr_number: int) -> CheckRollup:
        pr = self._pr_by_number(pr_number)
        if pr is None:
            return CheckRollup(
                pr_number=pr_number, head_sha="", head_pushed_at=datetime.now(UTC),
                branch_protection_readable=True, checks=[], base_ref="main",
            )
        return await self._ci_rollup(pr)

    async def _ci_rollup(self, pr: dict[str, Any]) -> CheckRollup:
        head_sha = await self._rev(pr["head_ref"])
        cached = self._ci_cache.get(head_sha)
        if cached is not None:
            return cached
        conclusion = await self._run_pytest(head_sha)
        rollup = CheckRollup(
            pr_number=pr["pr_number"],
            head_sha=head_sha,
            head_pushed_at=datetime.now(UTC),
            branch_protection_readable=True,
            checks=[CheckEntry(name="pytest", status="completed", conclusion=conclusion, is_required=True)],
            base_ref=pr["base_ref"] or "main",
            rollup_origin="head",
        )
        if head_sha:
            self._ci_cache[head_sha] = rollup
        return rollup

    async def _run_pytest(self, head_sha: str) -> Literal["success", "failure"]:
        """Real pytest against a clean checkout of ``head_sha``. Returns a
        CheckConclusion string ("success"/"failure")."""
        if not head_sha:
            return "failure"
        checkout = self._work_dir / f"ci-{head_sha[:12]}"
        try:
            def run() -> int:
                if not checkout.exists():
                    clone = subprocess.run(
                        ["git", "clone", "--quiet", str(self._bare_repo), str(checkout)],
                        capture_output=True, text=True, timeout=120,
                    )
                    if clone.returncode != 0:
                        return 1
                    co = subprocess.run(
                        ["git", "-C", str(checkout), "checkout", "--quiet", head_sha],
                        capture_output=True, text=True, timeout=60,
                    )
                    if co.returncode != 0:
                        return 1
                proc = subprocess.run(
                    list(self._test_command),
                    cwd=str(checkout), capture_output=True, text=True, timeout=300,
                )
                return proc.returncode

            exit_code = await asyncio.to_thread(run)
        except Exception as exc:
            logger.warning("fake_github.pytest_failed", head=head_sha[:12], error=str(exc))
            return "failure"
        self._record("ci_run", head_sha=head_sha, pytest_exit=exit_code)
        return "success" if exit_code == 0 else "failure"

    # ---- branches / repo / files ----------------------------------------

    async def branch_exists(self, branch_name: str) -> bool:
        return bool(await self._rev(branch_name))

    async def branch_has_open_pr(self, branch_name: str) -> bool | None:
        return any(pr["head_ref"] == branch_name and not pr["merged"] for pr in self._prs.values())

    async def delete_branch(self, branch_name: str) -> None:
        try:
            await self._git("branch", "-D", branch_name)
            self._record("delete_branch", branch=branch_name)
        except Exception as exc:
            logger.warning("fake_github.delete_branch_failed", branch=branch_name, error=str(exc))

    async def get_repository_id(self, owner: str, repo: str) -> str:
        return f"R_{owner}_{repo}"

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        try:
            r = await self._git("show", f"{ref}:{path}")
            return r.stdout if r.returncode == 0 else None
        except Exception as exc:
            logger.warning("fake_github.get_file_content_failed", path=path, error=str(exc))
            return None

    async def get_file_blob_sha(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        oid = await self._rev(f"{ref}:{path}")
        return oid or None

    async def list_prs_by_branch_prefix(
        self, owner: str, repo: str, prefix: str, state: str = "OPEN", limit: int = 20
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        want_open = state.upper() == "OPEN"
        for pr in self._prs.values():
            if not pr["head_ref"].startswith(prefix):
                continue
            if want_open and pr["merged"]:
                continue
            out.append({"id": pr["pr_id"], "url": pr["url"], "head_ref": pr["head_ref"]})
        return out[:limit]

    # ---- labels ----------------------------------------------------------

    async def get_label_ids(self, owner: str, repo: str) -> dict[str, str]:
        return {}

    async def ensure_labels_exist(
        self, owner: str, repo: str, handled_label: str, escalation_label: str
    ) -> dict[str, str]:
        return {handled_label: f"LA_{handled_label}", escalation_label: f"LA_{escalation_label}"}

    async def add_labels(self, issue_id: str, label_ids: list[str]) -> None:
        self._record("add_labels", issue_id=issue_id, labels=label_ids)

    # ---- approval / merge internals --------------------------------------

    def _review_decision(self, pr: dict[str, Any]) -> str:
        # 151 review fix: only a configured HUMAN reviewer's APPROVE counts as
        # APPROVED. This mirrors GitHub's reviewDecision (an unrequested bot's
        # approve does not satisfy it) and monitor_pr, which filters non-human
        # reviews out via classify_reviewer. Without the filter, the reviewer
        # performer's own `POST .../reviews` APPROVE (routed through
        # FakeGitHubServer._h_create_review) marks the PR APPROVED, which makes
        # _maybe_approve early-return forever — so the human approval is never
        # appended, monitor_pr sees zero human approvals, and the card can never
        # merge, while check_mergeability simultaneously reports mergeable=True.
        # CHANGES_REQUESTED is deliberately NOT filtered: a reviewer performer
        # must still be able to block its own PR.
        humans = {r.strip().lower() for r in self._human_reviewers}
        states = [str(r.get("state", "")).upper() for r in pr["reviews"]]
        human_states = [
            str(r.get("state", "")).upper()
            for r in pr["reviews"]
            if str(r.get("author_login", "")).strip().lower() in humans
        ]
        if "APPROVED" in human_states:
            return "APPROVED"
        if "CHANGES_REQUESTED" in states:
            return "CHANGES_REQUESTED"
        return "REVIEW_REQUIRED"

    async def _maybe_approve(self, pr: dict[str, Any]) -> None:
        """Consult the injected approver; on approval, append a human APPROVED review.

        The approver sees a fake-observable ``pr_state`` — CI-green plus the card's
        board column (resolves analyze finding U1): gates_green approves once CI is
        green AND coordinare has advanced the card to IN_REVIEW.
        """
        if self._review_decision(pr) == "APPROVED":
            return
        try:
            rollup = await self._ci_rollup(pr)
            ci_green = bool(rollup.checks) and all(
                c.conclusion == "success" for c in rollup.checks if c.is_required
            )
            card = self._cards.get(pr["issue_item_id"], {})
            state = {
                "ci_green": ci_green,
                "card_status": card.get("status", ""),
                "reviews": list(pr["reviews"]),
                "head_sha": rollup.head_sha,
            }
            if self._approver(state):
                login = self._human_reviewers[0] if self._human_reviewers else "bench-approver"
                pr["reviews"].append({
                    "id": f"REV_{pr['pr_id']}_{len(pr['reviews'])}",
                    "author_login": login,
                    "state": "APPROVED",
                    "body": "Approved by benchmark approver policy.",
                    "submitted_at": datetime.now(UTC).isoformat(),
                    "comments": [],
                    "commit_oid": rollup.head_sha,
                })
                self._record("approve", pr_id=pr["pr_id"], approved_by=login)
        except Exception as exc:
            logger.warning("fake_github.maybe_approve_failed", pr_id=pr.get("pr_id"), error=str(exc))

    async def _do_local_merge(self, pr: dict[str, Any]) -> str:
        """Squash-merge head into base in the bare repo (real git). Returns the
        merge commit oid, or "" on failure."""
        tmp = Path(tempfile.mkdtemp(prefix="bench-merge-", dir=self._work_dir))
        try:
            def run() -> str:
                base, head = pr["base_ref"], pr["head_ref"]
                steps = [
                    ["git", "clone", "--quiet", str(self._bare_repo), str(tmp)],
                    ["git", "-C", str(tmp), "checkout", "--quiet", base],
                    ["git", "-C", str(tmp), "merge", "--squash", f"origin/{head}"],
                    ["git", "-C", str(tmp), "-c", "user.email=bench@local",
                     "-c", "user.name=bench", "commit", "-m", f"Squash merge {head}"],
                    ["git", "-C", str(tmp), "push", "--quiet", "origin", base],
                ]
                for step in steps:
                    r = subprocess.run(step, capture_output=True, text=True, timeout=120)
                    if r.returncode != 0:
                        logger.warning("fake_github.merge_step_failed", step=step[-1], err=r.stderr[:200])
                        return ""
                rev = subprocess.run(
                    ["git", "-C", str(tmp), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
                )
                return rev.stdout.strip() if rev.returncode == 0 else ""

            return await asyncio.to_thread(run)
        except Exception as exc:
            logger.warning("fake_github.merge_failed", pr_id=pr.get("pr_id"), error=str(exc))
            return ""
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
