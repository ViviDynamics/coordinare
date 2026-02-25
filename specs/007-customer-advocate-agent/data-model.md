# Data Model: Customer Advocate Agent (007)

**Feature**: 007-customer-advocate-agent
**Date**: 2026-02-24

---

## Enums

### IssueType

Classification of a GitHub issue's intent.

```python
class IssueType(str, Enum):
    question = "question"
    confusion = "confusion"
    complaint = "complaint"
    feature_request = "feature_request"
    bug_report = "bug_report"
    off_topic = "off_topic"
```

### AdvocateAction

Action taken by the advocate for an issue.

```python
class AdvocateAction(str, Enum):
    replied = "replied"           # question/confusion: auto-reply posted
    escalated = "escalated"       # complaint/low-confidence/sensitive: human notified
    acknowledged = "acknowledged" # feature_request: acknowledgement posted
    triaged = "triaged"           # bug_report: no comment, recorded only
    redirected = "redirected"     # off_topic: redirect comment posted
```

### EscalationReason

Why human escalation was triggered.

```python
class EscalationReason(str, Enum):
    low_confidence = "low_confidence"
    sensitive_keyword = "sensitive_keyword"
    complaint = "complaint"
    no_documentation_match = "no_documentation_match"
    no_documentation_configured = "no_documentation_configured"
    claude_api_failure = "claude_api_failure"
```

---

## Entities

### IssueClassification

Assessment of a GitHub issue's type and confidence.

| Field | Type | Description |
|-------|------|-------------|
| `issue_id` | `str` | GitHub GraphQL node ID of the issue |
| `issue_number` | `int` | GitHub issue number (human-readable) |
| `classification` | `IssueType` | Detected category |
| `confidence_score` | `float` | Final consensus score (0.0–1.0, mean of all providers) |
| `sensitive_flagged` | `bool` | True if a sensitive keyword matched issue title/body |
| `classified_at` | `datetime` (UTC) | Timestamp of classification |

**Validation rules**:
- `confidence_score` ∈ `[0.0, 1.0]`
- `issue_id` must be non-empty
- `classified_at` must be timezone-aware (UTC)

---

### AdvocateResponse

An action taken for a GitHub issue.

| Field | Type | Description |
|-------|------|-------------|
| `issue_id` | `str` | GitHub GraphQL node ID |
| `action` | `AdvocateAction` | Action taken |
| `response_text` | `str \| None` | Comment text posted; null for `triaged` |
| `source_documents` | `list[str]` | File paths cited (empty for non-answer actions) |
| `confidence_score` | `float` | Score at time of action |
| `created_at` | `datetime` (UTC) | Timestamp of action |

---

### EscalationRecord

A human escalation event.

| Field | Type | Description |
|-------|------|-------------|
| `issue_id` | `str` | GitHub GraphQL node ID |
| `issue_number` | `int` | GitHub issue number |
| `reason` | `EscalationReason` | Why escalation was triggered |
| `notified_channel` | `str` | `"slack"` or `"email"` |
| `escalated_at` | `datetime` (UTC) | Timestamp of escalation |

---

### DocumentationSource

A configured documentation file fetched per poll cycle.

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | `str` | Repository-relative path (e.g., `"README.md"`) |
| `branch_ref` | `str` | Git ref used for fetch (e.g., `"main"`) |
| `content` | `str \| None` | File text content; null if fetch failed |
| `last_read_at` | `datetime` (UTC) | Timestamp of last fetch attempt |
| `reachable` | `bool` | False if fetch returned null or error |

---

### ScoringProvider

Single LLM provider's confidence assessment.

| Field | Type | Description |
|-------|------|-------------|
| `provider_name` | `str` | e.g., `"claude"`, `"openai"` |
| `score` | `float` | Confidence score (0.0–1.0) from this provider |
| `reasoning` | `str` | Provider's explanation of the score |

**Validation rules**:
- `score` ∈ `[0.0, 1.0]`
- `provider_name` must be non-empty

---

### ConsensusScore

Aggregated confidence result across all configured providers.

| Field | Type | Description |
|-------|------|-------------|
| `final_score` | `float` | Mean of all provider scores (0.0–1.0) |
| `provider_scores` | `list[ScoringProvider]` | Per-provider breakdown |

**Computation**: `final_score = mean(p.score for p in provider_scores)`. If all providers fail, `final_score = 0.0` → triggers escalation.

---

## Configuration Model

### AdvocateConfig

Nested Pydantic model added to `ProjectConfiguration`. All fields have defaults so the block is optional in `config.yaml`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Master on/off switch (FR-016) |
| `confidence_threshold` | `float` | `0.70` | Minimum score to auto-reply (FR-006) |
| `sensitive_keywords` | `list[str]` | See FR-007 defaults | Force-escalate matching issues |
| `doc_sources` | `list[str]` | `["README.md"]` | Repository-relative doc file paths |
| `scoring_models` | `list[str]` | `["claude"]` | LLM providers; V1 supports `"claude"` only |
| `handled_label` | `str` | `"advocate-handled"` | Applied when advocate takes action |
| `escalation_label` | `str` | `"needs-human"` | Applied when escalating |
| `holding_comment_template` | `str` | `"Thanks for reaching out — a team member will follow up shortly."` | Holding comment for escalations |
| `acknowledgement_template` | `str` | `"Thanks for the feature request! We've noted it for our roadmap."` | For feature_request issues |
| `redirect_template` | `str` | `"This doesn't seem related to the project. For support, please visit {support_channel_url}."` | For off_topic issues |
| `disclosure_template` | `str` | `"🤖 This response was generated automatically — please verify before acting on it."` | Appended to all AI responses |
| `support_channel_url` | `str` | `""` | URL for off-topic redirect |
| `github_repo` | `str` | `""` | Repository name (e.g., `"coordinare"`); required when enabled |

**Validation rules**:
- `confidence_threshold` ∈ `(0.0, 1.0]`
- When `enabled = True`: `github_repo` must be non-empty
- `scoring_models` must contain at least `"claude"` in V1

**Default sensitive keywords** (FR-007): `["billing", "payment", "legal", "security", "breach", "abuse", "harassment", "lawsuit", "GDPR", "refund"]`

---

## State Extensions

The following fields are added to `CoordinareState` (`src/coordinare/graph/state.py`):

| Field | Type | Description |
|-------|------|-------------|
| `advocate_service` | `AdvocateServiceProtocol \| None` | Injected at startup; None when `advocate.enabled = False` |
| `advocate_history` | `set[str]` | In-memory set of processed issue IDs (secondary dedup guard per FR-010a) |

**AdvocateServiceProtocol** (added to `state.py`):

```python
class AdvocateServiceProtocol(Protocol):
    async def scan_and_respond(
        self,
        processed_ids: set[str],
    ) -> set[str]:
        """Scan open issues, process unhandled ones, return updated processed_ids set."""
        ...
```

---

## Config YAML Example

```yaml
advocate:
  enabled: true
  confidence_threshold: 0.70
  doc_sources:
    - README.md
    - docs/quickstart.md
  sensitive_keywords:
    - billing
    - security
    - legal
  scoring_models:
    - claude
  support_channel_url: "https://github.com/org/coordinare/discussions"
  github_repo: "coordinare"
```

---

## Decision Flow

```
Issue detected (not in processed_ids, not labeled)
    │
    ├─ sensitive keyword match? ──► YES ──► apply needs-human label
    │                                       post holding comment
    │                                       notify human reviewer
    │                                       record EscalationRecord
    │
    └─ NO: call ClaudeScorer (classify + score)
           │
           ├─ API failure ──────────────► treat as low_confidence (reason: claude_api_failure)
           │                              → escalation path
           │
           ├─ classification = complaint ► escalation path (regardless of score)
           │
           ├─ confidence < threshold ────► escalation path (reason: low_confidence)
           │
           ├─ classification = question/confusion
           │      └─ no doc match ────────► escalation path (reason: no_documentation_match)
           │      └─ doc match + score ≥ threshold
           │              └─ apply advocate-handled label
           │                 post answer comment (with citation + disclosure)
           │                 record AdvocateResponse(action=replied)
           │
           ├─ classification = feature_request
           │      └─ apply advocate-handled label
           │         post acknowledgement comment
           │         record AdvocateResponse(action=acknowledged)
           │
           ├─ classification = bug_report
           │      └─ apply advocate-handled label
           │         NO comment posted
           │         record AdvocateResponse(action=triaged)
           │
           └─ classification = off_topic
                  └─ apply advocate-handled label
                     post redirect comment
                     record AdvocateResponse(action=redirected)
```
