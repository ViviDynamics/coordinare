# Contract: ScoringProvider Protocol

**Feature**: 007-customer-advocate-agent
**Module**: `src/coordinare/services/scoring.py`
**Date**: 2026-02-24

---

## Protocol Definition

```python
from __future__ import annotations
from typing import Protocol
from coordinare.models.advocate import ScoringProvider


class ScoringProviderProtocol(Protocol):
    """Interface for a single LLM confidence scoring provider.

    V1 implementation: ClaudeScorer
    Future implementations: OpenAIScorer, GitHubCopilotScorer

    Each provider receives the issue and documentation context and returns
    a structured assessment including a confidence score and the classification
    + response text (in V1, bundled in a single Claude API call).
    """

    @property
    def provider_name(self) -> str:
        """Unique identifier for this provider (e.g., "claude", "openai")."""
        ...

    async def score(
        self,
        issue_title: str,
        issue_body: str,
        doc_content: str,
    ) -> ScoringResult:
        """Classify the issue and score response confidence.

        Args:
            issue_title: The GitHub issue title.
            issue_body: The GitHub issue body (Markdown text).
            doc_content: Concatenated text of all reachable documentation files.

        Returns:
            ScoringResult with classification, confidence, response_text,
            source_documents, and the ScoringProvider record.

        On API failure:
            Must NOT raise. Return ScoringResult with confidence=0.0,
            reasoning="api_error", classification=None, response_text=None.
        """
        ...
```

---

## ScoringResult (return type)

Returned by every `ScoringProviderProtocol.score()` call. Contains both the scoring data and the generated response (V1 combined call).

```python
from dataclasses import dataclass
from coordinare.models.advocate import IssueType, ScoringProvider


@dataclass
class ScoringResult:
    provider: ScoringProvider           # Name + score + reasoning
    classification: IssueType | None    # None on API failure
    response_text: str | None           # Generated answer; None for non-answer classifications
    source_documents: list[str]         # Doc file paths cited in response_text
```

---

## V1: ClaudeScorer

The V1 implementation using the existing `ClaudeService`.

### Prompt Template

```
System: You are a customer advocate for a software project. Analyze the following
GitHub issue against the provided documentation.

Return ONLY a JSON object with these exact fields:
{
  "classification": "<question|confusion|complaint|feature_request|bug_report|off_topic>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<brief explanation of confidence score>",
  "response_text": "<answer citing documentation, or null>",
  "source_documents": ["<filename>", ...]
}

Rules:
- response_text must be null unless classification is "question" or "confusion"
  AND confidence >= 0.70 AND documentation covers the topic
- Every response_text MUST include a citation in the format "Based on `<filename>`:..."
- source_documents lists only files actually cited in response_text
- If documentation does not cover the topic, set confidence < 0.50 and
  response_text to null

Documentation:
{doc_content}

Issue title: {issue_title}

Issue body:
{issue_body}
```

### Response Parsing

```python
try:
    data = json.loads(response_text)
    classification = IssueType(data["classification"])
    confidence = float(data["confidence"])
    confidence = max(0.0, min(1.0, confidence))  # clamp to [0,1]
    response_text = data.get("response_text")
    source_documents = list(data.get("source_documents", []))
except (json.JSONDecodeError, KeyError, ValueError):
    # On parse failure, treat as API error
    return ScoringResult(
        provider=ScoringProvider(
            provider_name="claude", score=0.0, reasoning="parse_error"
        ),
        classification=None,
        response_text=None,
        source_documents=[],
    )
```

### Error Contract

- Claude API errors (network, rate limit, timeout): return `ScoringResult` with `confidence=0.0, reasoning="api_error"`.
- MUST NOT raise exceptions; caller (`AdvocateService`) treats `confidence=0.0` as escalation trigger.

---

## Aggregation (Current and Future)

```python
async def compute_consensus(
    providers: list[ScoringProviderProtocol],
    issue_title: str,
    issue_body: str,
    doc_content: str,
) -> tuple[ConsensusScore, ScoringResult]:
    """Run all providers concurrently and aggregate results.

    Returns the ConsensusScore and the primary provider's ScoringResult
    (classification + response_text from the first configured provider,
    typically Claude).
    """
    results = await asyncio.gather(
        *[p.score(issue_title, issue_body, doc_content) for p in providers],
        return_exceptions=True,
    )
    valid: list[ScoringResult] = [
        r for r in results if isinstance(r, ScoringResult)
    ]
    scores = [r.provider.score for r in valid]
    final_score = sum(scores) / len(scores) if scores else 0.0

    consensus = ConsensusScore(
        final_score=final_score,
        provider_scores=[r.provider for r in valid],
    )
    # Primary result (classification + response_text) comes from first provider
    primary = valid[0] if valid else ScoringResult(
        provider=ScoringProvider(provider_name="none", score=0.0, reasoning="all_failed"),
        classification=None,
        response_text=None,
        source_documents=[],
    )
    return consensus, primary
```

**On partial failure**: Use mean of successful providers only. If all fail, `final_score = 0.0` → advocate escalates.

---

## Adding a New Provider (Future)

1. Create `src/coordinare/services/scoring_{name}.py` implementing `ScoringProviderProtocol`.
2. Add the provider name to `scoring_models` in `config.yaml` (e.g., `["claude", "openai"]`).
3. Update `AdvocateService.__init__` to instantiate the new scorer when its name appears in `scoring_models`.
4. No changes to the advocate node, existing providers, or tests for other providers are required.

### Provider Registry Pattern (in AdvocateService)

```python
def _build_scorers(
    self,
    scoring_models: list[str],
    claude_service: ClaudeService,
) -> list[ScoringProviderProtocol]:
    registry: dict[str, ScoringProviderProtocol] = {
        "claude": ClaudeScorer(claude_service),
        # "openai": OpenAIScorer(openai_client),  # future
    }
    return [registry[name] for name in scoring_models if name in registry]
```

---

## Max Tokens Budget

| Provider | Classification call | Max response tokens |
|----------|---------------------|---------------------|
| Claude (V1) | Combined | 512 tokens |

Response text is bounded to ≤400 tokens to keep GitHub comments concise and avoid exceeding the Claude API budget per issue.
