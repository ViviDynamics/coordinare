"""Customer advocate agent service (007)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.metrics import METRICS
from coordinare.models.advocate import (
    AdvocateAction,
    DocumentationSource,
    EscalationReason,
    IssueType,
)
from coordinare.services.scoring import ScoringProviderProtocol, compute_consensus

if TYPE_CHECKING:
    from coordinare.config import AdvocateConfig

logger = structlog.get_logger(__name__)


class AdvocateService:
    """Scans open GitHub issues and responds based on AI classification."""

    def __init__(
        self,
        github: Any,
        notification_service: Any,
        config: AdvocateConfig,
        github_org: str,
        label_ids: dict[str, str] | None = None,
        scorers: list[ScoringProviderProtocol] | None = None,
    ) -> None:
        self._github = github
        self._notification_service = notification_service
        self._config = config
        self._org = github_org
        self._label_ids: dict[str, str] = label_ids or {}
        self._scorers: list[ScoringProviderProtocol] = scorers or []

    async def scan_and_respond(
        self,
        processed_ids: set[str],
        *,
        persona_instructions: str = "",
    ) -> set[str]:
        """Scan open issues, process unhandled ones, return updated processed_ids set.

        Args:
            processed_ids: Set of already-processed issue IDs to skip.
            persona_instructions: Optional behavioral instructions for the advocate
                role (018-performer-personas). Forwarded to scorers as additional context.
        """
        start = monotonic()
        cycle_id = f"cycle-{int(start)}"

        # Load documentation sources
        doc_sources = await self._load_documentation_sources()

        # List open issues
        try:
            issues = await self._github.list_open_issues(
                self._org, self._config.github_repo, first=20
            )
        except Exception as exc:
            logger.error("advocate_scan.list_issues_failed", error=str(exc))
            return processed_ids

        # Filter labeled/processed
        advocate_labels = {self._config.handled_label, self._config.escalation_label}
        unprocessed = []
        for issue in issues:
            issue_id = str(issue.get("id", ""))
            if issue_id in processed_ids:
                continue
            label_nodes = (issue.get("labels") or {}).get("nodes", [])
            label_names = {str(n.get("name", "")) for n in label_nodes if isinstance(n, dict)}
            if label_names & advocate_labels:
                continue
            unprocessed.append(issue)

        logger.info(
            "advocate_scan_start",
            cycle_id=cycle_id,
            open_issues_fetched=len(issues),
            unprocessed_count=len(unprocessed),
        )

        # Process up to 20 issues with Semaphore(5)
        sem = asyncio.Semaphore(5)
        new_ids: set[str] = set()

        async def _process_one(issue: dict[str, Any]) -> None:
            async with sem:
                issue_id = str(issue.get("id", ""))
                try:
                    await self._process_issue(
                        issue, doc_sources, persona_instructions=persona_instructions
                    )
                    new_ids.add(issue_id)
                except Exception as exc:
                    logger.error(
                        "advocate_issue_processing_failed",
                        issue_id=issue_id,
                        error=str(exc),
                    )

        await asyncio.gather(*[_process_one(issue) for issue in unprocessed])

        updated_ids = processed_ids | new_ids
        elapsed_ms = (monotonic() - start) * 1000
        logger.info(
            "advocate_scan_complete",
            cycle_id=cycle_id,
            issues_processed=len(new_ids),
            elapsed_ms=round(elapsed_ms),
        )
        METRICS.advocate_scan_duration_seconds.observe(monotonic() - start)

        return updated_ids

    async def _load_documentation_sources(self) -> list[DocumentationSource]:
        sources = []
        ref = self._config.doc_branch
        for path in self._config.doc_sources:
            try:
                content = await self._github.get_file_content(
                    self._org, self._config.github_repo, path, ref=ref
                )
                sources.append(
                    DocumentationSource(
                        file_path=path,
                        branch_ref=ref,
                        content=content,
                        last_read_at=datetime.now(UTC),
                        reachable=content is not None,
                    )
                )
                if content is None:
                    logger.warning(
                        "advocate_doc_fetch_warning",
                        file_path=path,
                        ref=ref,
                        error="null content",
                    )
            except Exception as exc:
                logger.warning(
                    "advocate_doc_fetch_warning",
                    file_path=path,
                    ref=ref,
                    error=str(exc),
                )
                sources.append(
                    DocumentationSource(
                        file_path=path,
                        branch_ref=ref,
                        content=None,
                        last_read_at=datetime.now(UTC),
                        reachable=False,
                    )
                )
        return sources

    async def _process_issue(
        self,
        issue: dict[str, Any],
        doc_sources: list[DocumentationSource],
        *,
        persona_instructions: str = "",
    ) -> None:
        issue_id = str(issue.get("id", ""))
        issue_number = int(issue.get("number", 0))
        title = str(issue.get("title", ""))
        body = str(issue.get("body", "") or "")
        issue_url = str(issue.get("url", f"#{issue_number}"))

        start = monotonic()

        # Step 1: Sensitive keyword check (before Claude call)
        text_to_check = f"{title} {body}".lower()
        for keyword in self._config.sensitive_keywords:
            if keyword.lower() in text_to_check:
                await self._do_escalate(
                    issue_id, issue_number, EscalationReason.sensitive_keyword, issue_url=issue_url
                )
                elapsed_ms = (monotonic() - start) * 1000
                logger.info(
                    "advocate_issue_processed",
                    issue_id=issue_id,
                    issue_number=issue_number,
                    classification=None,
                    action=AdvocateAction.escalated.value,
                    confidence=0.0,
                    provider_scores=[],
                    elapsed_ms=round(elapsed_ms),
                )
                return

        # Step 2: Gather reachable docs
        reachable_docs = [s for s in doc_sources if s.reachable and s.content]
        doc_content = (
            "\n\n".join(f"# {s.file_path}\n{s.content}" for s in reachable_docs)
            if reachable_docs
            else ""
        )

        # Step 3: Call scorers (prepend persona instructions when set, 018-performer-personas)
        effective_doc_content = doc_content
        if persona_instructions.strip():
            effective_doc_content = (
                f"## Advocate Instructions\n{persona_instructions.strip()}\n\n{doc_content}"
                if doc_content
                else f"## Advocate Instructions\n{persona_instructions.strip()}"
            )
        consensus, primary = await compute_consensus(
            self._scorers, title, body, effective_doc_content
        )
        classification = primary.classification
        confidence = consensus.final_score

        elapsed_ms = (monotonic() - start) * 1000

        provider_scores = [
            {"provider": p.provider_name, "score": p.score}
            for p in consensus.provider_scores
        ]

        # API failure (all providers failed)
        if classification is None:
            await self._do_escalate(
                issue_id, issue_number, EscalationReason.claude_api_failure, issue_url=issue_url
            )
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=None,
                action=AdvocateAction.escalated.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )
            return

        # Complaint → escalate regardless of confidence
        if classification == IssueType.complaint:
            await self._do_escalate(
                issue_id, issue_number, EscalationReason.complaint, issue_url=issue_url
            )
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=classification.value,
                action=AdvocateAction.escalated.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )
            return

        # Question / confusion
        if classification in (IssueType.question, IssueType.confusion):
            if not reachable_docs:
                await self._do_escalate(
                    issue_id,
                    issue_number,
                    EscalationReason.no_documentation_configured,
                    issue_url=issue_url,
                )
            elif primary.response_text is None:
                await self._do_escalate(
                    issue_id,
                    issue_number,
                    EscalationReason.no_documentation_match,
                    issue_url=issue_url,
                )
            elif confidence < self._config.confidence_threshold:
                # Low confidence: model returned a response but we're not confident
                # enough to post it. Applied only to question/confusion — feature
                # requests, bug reports, and off-topic issues follow their own paths
                # regardless of score.
                await self._do_escalate(
                    issue_id,
                    issue_number,
                    EscalationReason.low_confidence,
                    issue_url=issue_url,
                )
            else:
                await self._apply_label(issue_id, self._config.handled_label)
                comment_body = (
                    f"{primary.response_text}\n\n{self._config.disclosure_template}"
                )
                await self._post_comment(issue_id, comment_body)
                METRICS.advocate_issues_processed_total.labels(action="replied").inc()
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=classification.value,
                action=AdvocateAction.replied.value if primary.response_text else AdvocateAction.escalated.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )
            return

        # Feature request
        if classification == IssueType.feature_request:
            await self._apply_label(issue_id, self._config.handled_label)
            await self._post_comment(issue_id, self._config.acknowledgement_template)
            METRICS.advocate_issues_processed_total.labels(action="acknowledged").inc()
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=classification.value,
                action=AdvocateAction.acknowledged.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )
            return

        # Bug report (label only, no comment)
        if classification == IssueType.bug_report:
            await self._apply_label(issue_id, self._config.handled_label)
            METRICS.advocate_issues_processed_total.labels(action="triaged").inc()
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=classification.value,
                action=AdvocateAction.triaged.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )
            return

        # Off-topic
        if classification == IssueType.off_topic:
            await self._apply_label(issue_id, self._config.handled_label)
            redirect_body = self._config.redirect_template.format(
                support_channel_url=self._config.support_channel_url
            )
            await self._post_comment(issue_id, redirect_body)
            METRICS.advocate_issues_processed_total.labels(action="redirected").inc()
            logger.info(
                "advocate_issue_processed",
                issue_id=issue_id,
                issue_number=issue_number,
                classification=classification.value,
                action=AdvocateAction.redirected.value,
                confidence=confidence,
                provider_scores=provider_scores,
                elapsed_ms=round(elapsed_ms),
            )

    async def _apply_label(self, issue_id: str, label_name: str) -> None:
        label_id = self._label_ids.get(label_name, "")
        if label_id:
            try:
                await self._github.add_labels(issue_id, [label_id])
            except Exception as exc:
                logger.error("advocate_add_label_failed", issue_id=issue_id, label=label_name, error=str(exc))

    async def _post_comment(self, issue_id: str, body: str) -> None:
        try:
            await self._github.add_comment(issue_id, body)
        except Exception as exc:
            logger.error("advocate_add_comment_failed", issue_id=issue_id, error=str(exc))

    async def _do_escalate(
        self,
        issue_id: str,
        issue_number: int,
        reason: EscalationReason,
        issue_url: str = "",
    ) -> None:
        # Apply escalation label first (label-first approach)
        await self._apply_label(issue_id, self._config.escalation_label)

        # Post holding comment
        await self._post_comment(issue_id, self._config.holding_comment_template)

        # Dispatch notification
        notified_channel = ""
        if self._notification_service is not None:
            from coordinare.models.notification import (
                EventType,
                NotificationEvent,
                NotificationSeverity,
            )

            try:
                await self._notification_service.dispatch(
                    NotificationEvent(
                        event_type=EventType.advocate_escalation,
                        severity=NotificationSeverity.warning,
                        source="advocate",
                        payload={
                            "event_type": EventType.advocate_escalation.value,
                            "severity": NotificationSeverity.warning.value,
                            "source": "advocate",
                            "issue_url": issue_url,
                            "issue_number": str(issue_number),
                            "reason": reason.value,
                            "summary": f"Issue #{issue_number} escalated: {reason.value}",
                        },
                        dedup_key=f"advocate_escalation:{issue_id}",
                    )
                )
                notified_channel = "notification_service"
            except Exception as exc:
                logger.error("advocate_escalation_notification_failed", error=str(exc))

        METRICS.advocate_issues_escalated_total.labels(reason=reason.value).inc()
        METRICS.advocate_issues_processed_total.labels(action="escalated").inc()
        logger.info(
            "advocate_issue_escalated",
            issue_id=issue_id,
            issue_number=issue_number,
            reason=reason.value,
            notified_channel=notified_channel,
        )
