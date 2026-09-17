"""WikiInitService — living project-wiki bootstrap gate (spec 124 US2).

Mirrors the env-bootstrap gate (spec 060/088): a persisted, circuit-broken,
restart-safe prerequisite that ensures a symphony's ``docs/wiki/`` record of
truth exists — seeded by our own documenter (``tech_writer``) in ``init`` mode —
before any non-documentation work is dispatched.

STATUS: this is the tested decision brain. Its daemon dispatch wiring (T026) is
NOT yet landed — it needs a cardless documenting job that opens a PR (a
performer-side change) plus a per-cycle daemon dispatch/poll, validated against
a live performer. Constructed disabled by default (``enabled=False``); nothing
in production instantiates it until T026 wires the gate + trigger together. See
``specs/124-openwiki-documenter/contracts/wiki-init-gate.md``.

Lifecycle (per symphony, state lives on ``EnvCacheState``):
  * ``needs_init`` — should a wiki-init job be dispatched this cycle?
  * ``gate_holds`` — should non-documentation dispatch be held?
  * ``mark_initialized`` — seed wiki merged to the default branch.
  * ``try_auto_merge`` — CI-green + trusted-bot approval → squash-merge the seed
    PR (the deliberate, scoped exception to the human-only merge gate; ongoing
    wiki updates keep the normal human review).
  * ``register_failure`` / ``notify_blocked`` — attempt budget + circuit breaker.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.notification import (
    EventType,
    NotificationEvent,
    NotificationSeverity,
)

if TYPE_CHECKING:
    from coordinare.models.env_cache import EnvCacheState

logger = structlog.get_logger(__name__)


class WikiInitService:
    """Gate + lifecycle for the living docs/wiki project-wiki initialization."""

    def __init__(self, *, enabled: bool = False, max_attempts: int = 3) -> None:
        self._enabled = bool(enabled)
        self._max_attempts = max(1, int(max_attempts))

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Gate decisions (pure)
    # ------------------------------------------------------------------

    def needs_init(self, ec_state: EnvCacheState, wiki_present_on_default_branch: bool) -> bool:
        """True when a wiki-init job should be dispatched this cycle.

        False when the gate is off, the wiki already exists (on-branch or via the
        persisted marker), a job is in flight, or the attempt budget is exhausted.
        """
        if not self._enabled:
            return False
        if ec_state.wiki_initialized or wiki_present_on_default_branch:
            return False
        return not (ec_state.wiki_in_flight or ec_state.wiki_exhausted)

    def gate_holds(self, ec_state: EnvCacheState) -> bool:
        """True when non-documentation dispatch must be held.

        Holds until the wiki is initialized. On exhaustion the hold persists
        (operator must act) — but ``notify_blocked`` has already fired, so this
        is a surfaced hold, never a silent deadlock (FR-017/SC-007).
        """
        return self._enabled and not ec_state.wiki_initialized

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def mark_initialized(self, ec_state: EnvCacheState) -> None:
        ec_state.wiki_initialized = True
        ec_state.wiki_in_flight = False
        ec_state.wiki_exhausted = False
        ec_state.last_wiki_init_succeeded = True
        ec_state.last_wiki_init_at = datetime.now(UTC)
        ec_state.last_wiki_init_error = None

    def register_failure(self, ec_state: EnvCacheState, error: str) -> bool:
        """Record a failed init attempt; return True if the budget is now spent."""
        ec_state.wiki_in_flight = False
        ec_state.wiki_attempts += 1
        ec_state.last_wiki_init_succeeded = False
        ec_state.last_wiki_init_error = error
        ec_state.last_wiki_init_at = datetime.now(UTC)
        if ec_state.wiki_attempts >= self._max_attempts:
            ec_state.wiki_exhausted = True
        return ec_state.wiki_exhausted

    # ------------------------------------------------------------------
    # Per-cycle trigger + completion (mirrors EnvCacheService.check_and_trigger)
    # ------------------------------------------------------------------

    async def check_and_trigger(
        self,
        symphony_name: str,
        ec_state: EnvCacheState | None,
        github: Any,
        org: str,
        repo: str,
        dispatch_fn: Any,
    ) -> None:
        """Once per cycle: dispatch a wiki-init job when the wiki is absent.

        ``dispatch_fn(symphony_name)`` performs the actual cardless documenting
        (``doc_mode="init"``) dispatch. This method owns only the DECISION:
        gate on/enabled, not already initialized/in-flight/exhausted, and no
        wiki yet on the default branch. On dispatch it sets ``wiki_in_flight``;
        the completion path (`handle_init_result`) clears it.
        """
        if not self._enabled or ec_state is None:
            return
        if ec_state.wiki_initialized or ec_state.wiki_in_flight or ec_state.wiki_exhausted:
            return
        if await self._wiki_present(github, org, repo):
            # A wiki already exists on the default branch (pre-seeded / prior
            # run) — adopt it without re-initializing.
            self.mark_initialized(ec_state)
            return
        ec_state.wiki_in_flight = True
        try:
            await dispatch_fn(symphony_name)
        except Exception as exc:  # a sync dispatch failure clears the in-flight flag
            self.register_failure(ec_state, f"wiki-init dispatch failed: {exc}")
            logger.warning("wiki_init.dispatch_failed", symphony=symphony_name, error=str(exc))

    async def _wiki_present(self, github: Any, org: str, repo: str) -> bool:
        """True when the default branch already carries the wiki entrypoint.

        ``docs/wiki/README.md`` is the documenter's canonical entrypoint (it links
        every section page), so its presence on the default branch is the marker
        that the living wiki has been initialized.
        """
        try:
            content = await github.get_file_content(org, repo, "docs/wiki/README.md")
            # Presence — not content — is the marker: an existing entrypoint (even
            # momentarily empty) means the wiki is initialized. get_file_content
            # returns None for an absent file, "" for an empty one; only None is
            # "not present" (bool("") would wrongly re-trigger init on an empty file).
            return content is not None
        except Exception:  # absence / API error → treat as not present
            return False

    async def handle_init_result(
        self,
        symphony_name: str,
        ec_state: EnvCacheState,
        github: Any,
        pr_node_id: str,
        trusted_bot_reviewers: list[str],
        notification_service: Any,
        *,
        job_succeeded: bool,
        error: str | None = None,
    ) -> None:
        """Resolve a completed wiki-init job: auto-merge the seed PR on success,
        else record a failure (and notify on exhaustion / auto-merge-blocked)."""
        if not job_succeeded:
            if self.register_failure(ec_state, error or "wiki-init job failed"):
                await self.notify_blocked(
                    notification_service, symphony_name, "wiki-init attempts exhausted", ec_state,
                )
            return
        if not pr_node_id:
            if self.register_failure(ec_state, "wiki-init produced no pull request"):
                await self.notify_blocked(
                    notification_service, symphony_name, "wiki-init produced no PR", ec_state,
                )
            return
        merged, reason = await self.try_auto_merge(
            github, pr_node_id, trusted_bot_reviewers, ec_state=ec_state,
        )
        if merged:
            self.mark_initialized(ec_state)
            return
        # Auto-merge blocked (e.g. branch protection) → surfaced hold + notify,
        # even before the budget is exhausted (SC-007: never a silent deadlock).
        self.register_failure(ec_state, f"seed-wiki PR not auto-merged: {reason}")
        await self.notify_blocked(
            notification_service, symphony_name, f"seed-wiki auto-merge blocked: {reason}", ec_state,
        )

    # ------------------------------------------------------------------
    # Seed-PR auto-merge (scoped exception to the human-only merge gate)
    # ------------------------------------------------------------------

    async def try_auto_merge(
        self,
        github: Any,
        pr_node_id: str,
        trusted_bot_reviewers: list[str],
        *,
        ec_state: EnvCacheState,
    ) -> tuple[bool, str]:
        """Auto-merge the init-bootstrap PR on required-CI-green + trusted-bot
        approval. Returns ``(merged, reason)``.

        Deliberately bypasses the normal human-approval merge gate — scoped to
        the single initialization PR (FR-015). Ongoing wiki updates keep the
        human review (FR-018); this must NEVER auto-merge a non-init PR.

        Guards (spec 124 review):
        * only runs while a wiki-init job is in flight (``ec_state.wiki_in_flight``)
          — prevents a misrouted ``pr_node_id`` from auto-merging an unrelated PR;
        * requires ``merge_state_status == "CLEAN"`` — the ONLY state that
          guarantees git-mergeable AND all required commit-status checks passing
          (UNSTABLE/BLOCKED/DIRTY/BEHIND/UNKNOWN/DRAFT all hold), so a red-CI PR
          can never auto-merge;
        * requires a trusted-bot approval.
        Branch protection requiring a human review keeps the PR out of CLEAN, so
        the caller degrades to hold + notify (the "auto-merge blocked" edge case).
        """
        if not ec_state.wiki_in_flight:
            return False, "no wiki-init in flight (auto-merge is init-bootstrap only)"

        merge = await github.check_mergeability(pr_node_id)
        mss = str(merge.get("merge_state_status", "")).upper()
        if mss != "CLEAN":
            return False, f"not CI-green (merge_state_status={mss or 'UNKNOWN'})"

        reviews = await github.get_pr_reviews(pr_node_id)
        trusted = set(trusted_bot_reviewers or [])
        approved_by_bot = any(
            str(r.get("state", "")).upper() == "APPROVED"
            and str(r.get("author_login", "")) in trusted
            for r in (reviews or [])
        )
        if not approved_by_bot:
            return False, "awaiting trusted-bot approval"

        result = await github.squash_merge(pr_node_id)
        if result.get("merged"):
            return True, "merged"
        return False, "squash_merge did not merge (branch protection?)"

    # ------------------------------------------------------------------
    # Operator notification
    # ------------------------------------------------------------------

    async def notify_blocked(
        self,
        notification_service: Any,
        symphony_name: str,
        reason: str,
        ec_state: EnvCacheState,
    ) -> None:
        """Emit a single critical, dedup-keyed operator notification (FR-017)."""
        if notification_service is None:
            return
        try:
            await notification_service.dispatch(
                NotificationEvent(
                    event_type=EventType.wiki_init_exhausted,
                    severity=NotificationSeverity.critical,
                    source="wiki_init",
                    payload={
                        "symphony": symphony_name,
                        "reason": reason,
                        "attempts": str(ec_state.wiki_attempts),
                        "last_error": str(ec_state.last_wiki_init_error or ""),
                    },
                    dedup_key=f"wiki_init_exhausted:{symphony_name}",
                ),
            )
        except Exception as exc:  # pragma: no cover — notification must never crash the gate
            logger.warning("wiki_init.notify_failed", symphony=symphony_name, error=str(exc))
