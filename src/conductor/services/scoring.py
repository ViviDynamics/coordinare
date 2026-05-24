"""ScoringProvider protocol and BackendScorer implementation (007).

The scorer delegates to the configured ConductingBackend so the choice of LLM
provider follows the coordinare's backend config (anthropic_api, openai_api,
claude_cli, codex_cli, opencode, none) rather than being hardcoded to the
Anthropic SDK.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from coordinare.models.advocate import ConsensusScore, IssueType, ScoringProvider

if TYPE_CHECKING:
    from coordinare.services.conducting import ConductingBackend


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


class BackendScorer:
    """Backend-agnostic scorer driven by a ConductingBackend.

    The previous ClaudeScorer reached into the Anthropic SDK directly. This
    version routes through ``backend.prompt(text, response_format="json")``,
    which already handles JSON parsing and works with any configured backend
    (Anthropic, OpenAI-compatible, claude CLI, codex CLI, opencode, hermes).
    """

    def __init__(
        self,
        backend: ConductingBackend,
        provider_name: str = "backend",
    ) -> None:
        self._backend = backend
        self._provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    async def score(
        self,
        issue_title: str,
        issue_body: str,
        doc_content: str,
    ) -> ScoringResult:
        _failure = ScoringResult(
            provider=ScoringProvider(
                provider_name=self._provider_name, score=0.0, reasoning="api_error",
            ),
            classification=None,
            response_text=None,
        )
        prompt = (
            f"{_SYSTEM_PROMPT}\n\n"
            f"Documentation:\n{doc_content}\n\n"
            f"Issue title: {issue_title}\n\n"
            f"Issue body:\n{issue_body}"
        )

        # One retry on parse_error: small open-source models occasionally drop
        # the JSON envelope on a first attempt but produce valid JSON on the
        # second. Backend-level (http) errors still propagate as api_error.
        data: Any = None
        for _attempt in range(2):
            try:
                response = await self._backend.prompt(prompt, response_format="json")
            except Exception:
                return _failure
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(data, dict):
                break

        if not isinstance(data, dict):
            return ScoringResult(
                provider=ScoringProvider(
                    provider_name=self._provider_name, score=0.0, reasoning="parse_error",
                ),
                classification=None,
                response_text=None,
            )

        try:
            classification = IssueType(data["classification"])
            confidence = float(data["confidence"])
            confidence = max(0.0, min(1.0, confidence))
            response_text = data.get("response_text")
            source_documents = list(data.get("source_documents", []))
        except (KeyError, ValueError, TypeError):
            return ScoringResult(
                provider=ScoringProvider(
                    provider_name=self._provider_name, score=0.0, reasoning="parse_error",
                ),
                classification=None,
                response_text=None,
            )

        return ScoringResult(
            provider=ScoringProvider(
                provider_name=self._provider_name,
                score=confidence,
                reasoning=str(data.get("reasoning", "")),
            ),
            classification=classification,
            response_text=response_text,
            source_documents=source_documents,
        )


# Backwards-compatible alias: existing call sites and tests reference ClaudeScorer.
ClaudeScorer = BackendScorer


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
