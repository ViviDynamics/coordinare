"""ScoringProvider protocol and ClaudeScorer implementation (007)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from coordinare.models.advocate import ConsensusScore, IssueType, ScoringProvider

if TYPE_CHECKING:
    from coordinare.services.claude import ClaudeService


@dataclass
class ScoringResult:
    provider: ScoringProvider
    classification: IssueType | None
    response_text: str | None
    source_documents: list[str] = field(default_factory=list)


class ScoringProviderProtocol(Protocol):
    @property
    def provider_name(self) -> str: ...

    async def score(
        self,
        issue_title: str,
        issue_body: str,
        doc_content: str,
    ) -> ScoringResult: ...


_SYSTEM_PROMPT = (
    "You are a customer advocate for a software project. Analyze the following "
    "GitHub issue against the provided documentation.\n\n"
    "Return ONLY a JSON object with these exact fields:\n"
    "{\n"
    '  "classification": "<question|confusion|complaint|feature_request|bug_report|off_topic>",\n'
    '  "confidence": <float 0.0-1.0>,\n'
    '  "reasoning": "<brief explanation of confidence score>",\n'
    '  "response_text": "<answer citing documentation, or null>",\n'
    '  "source_documents": ["<filename>", ...]\n'
    "}\n\n"
    "Rules:\n"
    '- response_text must be null unless classification is "question" or "confusion"\n'
    "  AND the documentation clearly covers the topic\n"
    '- Every response_text MUST include a citation in the format "Based on `<filename>`:..."\n'
    "- source_documents lists only files actually cited in response_text\n"
    "- If documentation does not cover the topic, set response_text to null and\n"
    "  clearly explain this in the reasoning field\n"
)


class ClaudeScorer:
    def __init__(self, claude_service: ClaudeService) -> None:
        self._claude = claude_service

    @property
    def provider_name(self) -> str:
        return "claude"

    async def score(
        self,
        issue_title: str,
        issue_body: str,
        doc_content: str,
    ) -> ScoringResult:
        _failure = ScoringResult(
            provider=ScoringProvider(provider_name="claude", score=0.0, reasoning="api_error"),
            classification=None,
            response_text=None,
        )
        try:
            user_message = (
                f"Documentation:\n{doc_content}\n\n"
                f"Issue title: {issue_title}\n\n"
                f"Issue body:\n{issue_body}"
            )
            response = await self._claude._client.messages.create(
                model=self._claude._model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception:
            return _failure

        text = ""
        if getattr(response, "content", None):
            blocks = response.content
            if blocks and hasattr(blocks[0], "text"):
                text = blocks[0].text
        if not text:
            return _failure

        try:
            data = json.loads(text)
            classification = IssueType(data["classification"])
            confidence = float(data["confidence"])
            confidence = max(0.0, min(1.0, confidence))
            response_text = data.get("response_text")
            source_documents = list(data.get("source_documents", []))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return ScoringResult(
                provider=ScoringProvider(
                    provider_name="claude", score=0.0, reasoning="parse_error"
                ),
                classification=None,
                response_text=None,
            )

        return ScoringResult(
            provider=ScoringProvider(
                provider_name="claude",
                score=confidence,
                reasoning=str(data.get("reasoning", "")),
            ),
            classification=classification,
            response_text=response_text,
            source_documents=source_documents,
        )


async def compute_consensus(
    providers: list[ScoringProviderProtocol],
    issue_title: str,
    issue_body: str,
    doc_content: str,
) -> tuple[ConsensusScore, ScoringResult]:
    """Run all providers concurrently and aggregate results."""
    results = await asyncio.gather(
        *[p.score(issue_title, issue_body, doc_content) for p in providers],
        return_exceptions=True,
    )
    valid: list[ScoringResult] = [r for r in results if isinstance(r, ScoringResult)]
    # Only include successful results (non-None classification) in score aggregation
    # so that parse_error/api_error failures don't pull the mean down.
    successful: list[ScoringResult] = [r for r in valid if r.classification is not None]
    scores = [r.provider.score for r in successful]
    final_score = sum(scores) / len(scores) if scores else 0.0

    consensus = ConsensusScore(
        final_score=final_score,
        provider_scores=[r.provider for r in successful],
    )
    primary = successful[0] if successful else ScoringResult(
        provider=ScoringProvider(provider_name="none", score=0.0, reasoning="all_failed"),
        classification=None,
        response_text=None,
    )
    return consensus, primary
