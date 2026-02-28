# Feature Specification: Customer Advocate Agent

**Feature Branch**: `007-customer-advocate-agent`
**Created**: 2026-02-23
**Status**: Draft
**Input**: User description: "A new LangGraph agent that monitors customer-facing GitHub issues for questions, complaints, or confusion; responds with empathetic, helpful answers sourced from project documentation and README; escalates to human reviewers when confidence is low or topic is sensitive; tracks response history to avoid duplicate replies; integrates with the coordinare board orchestrator workflow"

---

## User Scenarios & Testing

### User Story 1 — Auto-Reply to Customer Questions (Priority: P1)

A customer opens a GitHub issue with a question or confusion about the project. The coordinare detects the new issue on its next poll cycle, classifies it as a question or confusion, generates an empathetic and accurate answer sourced from the project documentation and README, and posts that answer as a GitHub comment — all without human intervention.

**Why this priority**: This is the core value proposition. Customers get fast, accurate answers from documentation without waiting for a human. Every poll cycle that passes without a response is a worse customer experience.

**Independent Test**: Create a GitHub issue with a documented question (e.g., "How do I configure the poll interval?"). Run one coordinare poll cycle. Verify a comment appears on the issue citing the relevant documentation section and answering the question accurately.

**Acceptance Scenarios**:

1. **Given** a new open GitHub issue containing a question about a topic covered in the project README, **When** the coordinare advocate node runs, **Then** it posts exactly one comment answering the question and cites the source documentation section.
2. **Given** an issue that was already responded to in a previous cycle, **When** the advocate node runs again, **Then** it does NOT post a second reply (duplicate prevention).
3. **Given** an issue that is already closed, **When** the advocate node scans for issues, **Then** it skips the closed issue without posting any comment.
4. **Given** a question issue with high confidence (≥ 0.70), **When** the advocate posts its answer, **Then** the comment includes the configured AI-disclosure line and a documentation citation.

---

### User Story 2 — Human Escalation for Low-Confidence or Sensitive Issues (Priority: P2)

When the advocate agent encounters an issue it cannot answer with high confidence, or an issue that touches on a sensitive topic (billing, security, legal, harassment, abuse), it does not auto-post a response. Instead, it routes the issue to a human reviewer via Slack or email notification and posts a holding comment on the issue indicating that a team member will follow up shortly.

**Why this priority**: Auto-posting an incorrect or inappropriate response to a sensitive issue is worse than no response. Escalation protects the project's reputation and ensures customers always receive an appropriate human answer for difficult topics.

**Independent Test**: Create a GitHub issue containing the word "billing". Run the coordinare. Verify: (a) no AI-generated answer is posted, (b) a human reviewer receives a Slack or email notification with the issue URL and escalation reason, (c) a holding comment appears on the issue.

**Acceptance Scenarios**:

1. **Given** a new issue whose title or body contains a configured sensitive keyword (e.g., "billing", "security"), **When** the advocate node runs, **Then** it escalates to human reviewers and posts a holding comment — it does NOT post an auto-generated answer.
2. **Given** a new question issue where the confidence score is below 0.70 (default threshold), **When** the advocate node runs, **Then** it escalates rather than posting a low-confidence answer.
3. **Given** a new issue classified as `complaint`, **When** the advocate node runs, **Then** it escalates regardless of confidence score.
4. **Given** an escalation is triggered, **When** the notification is dispatched, **Then** the human reviewer receives a message within one poll cycle containing the issue URL, issue classification, and escalation reason.
5. **Given** an issue with multiple questions where at least one generates a low-confidence answer, **When** the advocate processes it, **Then** the entire issue is escalated (partial answers are not posted).

---

### User Story 3 — Documentation-Sourced Answers (Priority: P3)

The advocate agent reads from a configurable set of documentation files (README, docs/ directory, CONTRIBUTING.md, CHANGELOG.md) to construct its answers. Every response cites the specific source file so customers can read the full context. If no relevant content is found in the configured documentation, the agent treats the issue as low-confidence and escalates.

**Why this priority**: Without grounding in actual project documentation, answers could be hallucinated and misleading. Source grounding is the quality control mechanism, but it can be added incrementally after the basic response loop (US1) is working.

**Independent Test**: Configure a documentation source set. Post an issue asking about a topic NOT covered in the documentation. Verify the agent escalates rather than hallucinating an answer. Post an issue about a documented topic. Verify the response cites the correct file.

**Acceptance Scenarios**:

1. **Given** a question issue about a topic present in configured documentation, **When** the advocate generates a response, **Then** the response includes a citation in the format "Based on `[filename]`:…".
2. **Given** a question issue about a topic absent from all configured documentation, **When** the advocate runs, **Then** it escalates with reason `no_documentation_match` rather than posting an unsourced answer.
3. **Given** a configured documentation path that is inaccessible, **When** the advocate runs, **Then** it logs a structured warning and escalates all issues in that poll cycle rather than failing silently or posting unsourced answers.

---

### User Story 4 — Feature-Request and Off-Topic Triage (Priority: P4)

The advocate agent classifies issues that are not questions or complaints — feature requests, bug reports, off-topic posts — and applies lightweight triage. Feature requests receive a configured acknowledgement comment. Bug reports are passed to the existing coordinare board workflow without an advocate comment. Off-topic posts receive a polite redirect.

**Why this priority**: Correct triage prevents the agent from ignoring non-question issues or misclassifying them, while avoiding noise for issue types handled by the main coordinare workflow.

**Independent Test**: Submit a feature request issue. Verify the agent posts a configured acknowledgement comment. Submit a bug report. Verify no advocate comment appears. Submit an off-topic issue. Verify a polite redirect comment appears.

**Acceptance Scenarios**:

1. **Given** a new issue classified as `feature_request`, **When** the advocate node runs, **Then** it posts the configured acknowledgement template and marks the issue as responded.
2. **Given** a new issue classified as `bug_report`, **When** the advocate node runs, **Then** it does NOT post any comment (the coordinare board workflow handles bug reports) and marks it as triaged.
3. **Given** a new issue classified as `off_topic`, **When** the advocate node runs, **Then** it posts a polite redirect comment pointing to the configured support channel URL.

---

### Edge Cases

- What happens when the GitHub API is unavailable when posting a reply? → Use the resilience layer (spec 005) for retry; if permanently failed, log a structured error and skip the issue in this cycle without crashing the advocate node.
- What happens if a sensitive keyword appears in the documentation text, not the issue itself? → The keyword match applies only to the issue title and body; documentation content does not trigger escalation.
- What happens if the human reviewer never responds to an escalation? → The agent does not auto-post after a timeout. It logs the unresolved escalation and continues; escalated issues remain open for human action indefinitely.
- What happens if the project has no documentation files at all? → The agent escalates all question/confusion issues with reason `no_documentation_configured` and logs a configuration warning.
- What happens if the Claude API fails during response generation? → Treat as low-confidence; escalate rather than partial-post a truncated answer.
- What happens if the same issue is updated (edited) after the agent already replied? → The agent does not re-respond to updates on issues it has already commented on.
- What happens if two coordinare poll cycles overlap and both detect the same new issue? → Duplicate reply prevention (in-memory response history) ensures only one advocate comment is posted.

---

## Requirements

### Functional Requirements

- **FR-001**: The coordinare MUST scan all open GitHub issues on every poll cycle and identify those not yet labeled `advocate-handled` or `needs-human`. Issues already carrying either label are skipped without processing.
- **FR-001a**: The board workflow MUST skip any issue labeled `advocate-handled` or `needs-human` to prevent the two workflows from acting on the same issue simultaneously.
- **FR-002**: The advocate agent MUST classify each unacted-upon issue into one of: `question`, `confusion`, `complaint`, `feature_request`, `bug_report`, or `off_topic` using the Claude model.
- **FR-003**: For `question` and `confusion` issues, the advocate MUST generate a response grounded in configured documentation sources. Documentation files are retrieved via the GitHub GraphQL API (using the existing GitHub service, via `repository.object(expression: "<ref>:<path>")`) by path and the configured `doc_branch` ref on each poll cycle — no local filesystem access is required.
- **FR-004**: Every generated response MUST include a citation of the source document(s) used (e.g., "Based on `README.md`:…").
- **FR-005**: The advocate MUST compute a confidence score (0.0–1.0) for each generated response using a pluggable scoring interface capable of querying one or more configured LLM providers and aggregating their scores (e.g., averaging). In V1, only Claude is implemented; the interface MUST be designed so additional providers (OpenAI, GitHub Copilot) can be added via configuration without code changes to the advocate node. Each provider returns a structured JSON payload including `{"confidence": 0.0–1.0, "reasoning": "..."}` alongside the response text. The final score is the mean of all providers that returned a **successful classification** (providers that returned a parse error or API failure are excluded from aggregation so transient failures do not artificially suppress confidence). The score and contributing provider scores MUST be recorded in the `advocate_issue_processed` structured log event.
- **FR-006**: If the confidence score is below the configured threshold (default: 0.70), the advocate MUST escalate and MUST NOT post the generated answer.
- **FR-007**: If the issue title or body contains any configured sensitive keyword (default list: "billing", "payment", "legal", "security", "breach", "abuse", "harassment", "lawsuit", "GDPR", "refund"), the advocate MUST escalate regardless of confidence score.
- **FR-008**: If classified as `complaint`, the advocate MUST escalate regardless of confidence score.
- **FR-009**: On escalation, the advocate MUST post a holding comment on the GitHub issue (configurable template, e.g., "Thanks for reaching out — a team member will follow up shortly") and send a notification via the configured notification channel (Slack or email).
- **FR-010**: Immediately upon detecting an unprocessed issue, the advocate MUST apply the appropriate GitHub label — `advocate-handled` when auto-replying, acknowledging, redirecting, or triaging (bug_report); `needs-human` when escalating — before posting any comment (or, for bug_report, before recording the triage). This label-first approach prevents duplicate processing if two poll cycles overlap.
- **FR-010a**: The advocate MUST maintain an in-memory response history (keyed by issue ID) within the current coordinare session as a secondary guard against duplicate posts, complementing the label-based approach.
- **FR-011**: For `feature_request` issues, the advocate MUST post the configured acknowledgement template and record the issue as acted upon.
- **FR-012**: For `bug_report` issues, the advocate MUST NOT post any comment; it MUST record the issue as triaged so it is not processed again in this session.
- **FR-013**: For `off_topic` issues, the advocate MUST post the configured redirect comment pointing to the support channel URL.
- **FR-014**: All advocate-generated comments MUST include the configured AI-disclosure line (default: "🤖 This response was generated automatically — please verify before acting on it") to satisfy transparency requirements.
- **FR-015**: The advocate node MUST be implemented as a LangGraph node within the existing coordinare graph, consistent with spec 001 architecture.
- **FR-016**: The advocate feature MUST be fully disableable via a single `enabled: false` config flag with zero impact on the poll cycle when disabled.
- **FR-017**: All configurable parameters — confidence threshold, sensitive keyword list, documentation source paths, response templates, disclosure text, support channel URL — MUST be settable via the coordinare config file with no code changes required.

### Key Entities

- **IssueClassification**: The agent's assessment of a GitHub issue — `issue_id`, `classification` (enum: question/confusion/complaint/feature_request/bug_report/off_topic), `confidence_score` (float 0.0–1.0), `sensitive_flagged` (bool), `classified_at` (datetime).
- **AdvocateResponse**: An action taken for an issue — `issue_id`, `action` (enum: replied/escalated/acknowledged/triaged/redirected), `response_text` (nullable), `source_documents` (list of file paths cited), `confidence_score` (float), `created_at` (datetime).
- **EscalationRecord**: A human escalation event — `issue_id`, `reason` (enum: low_confidence/sensitive_keyword/complaint/no_documentation_match/no_documentation_configured), `notified_channel` (slack/email), `escalated_at` (datetime).
- **DocumentationSource**: A configured documentation file — `file_path`, `branch_ref` (git ref used to fetch via GitHub Contents API), `last_read_at` (datetime), `reachable` (bool).
- **AdvocateConfig**: Configuration block — `enabled` (bool), `confidence_threshold` (float), `sensitive_keywords` (list[str]), `doc_sources` (list[str]), `doc_branch` (str, default: `"HEAD"`; git ref used when fetching documentation files via the GitHub GraphQL API — override if docs live on a dedicated branch), `scoring_models` (list[str], default: `["claude"]`; extensible to `["claude", "openai", "copilot"]`), `handled_label` (str, default: `"advocate-handled"`), `escalation_label` (str, default: `"needs-human"`), `holding_comment_template` (str), `acknowledgement_template` (str), `redirect_template` (str), `disclosure_template` (str), `support_channel_url` (str).
- **ScoringProvider**: Abstraction for a single LLM confidence scorer — `provider_name` (str), `score` (float 0.0–1.0), `reasoning` (str). Aggregated per issue into a `ConsensusScore` with `final_score` (mean) and `provider_scores` (list[ScoringProvider]).

---

## Success Criteria

### Measurable Outcomes

- **SC-001**: 100% of newly opened question/confusion issues receive an advocate action (reply, escalation, or holding comment) within one coordinare poll cycle of being opened.
- **SC-002**: Zero duplicate advocate comments posted to any single GitHub issue within a coordinare session.
- **SC-003**: 100% of issues with confidence score below the configured threshold, matching a sensitive keyword, or classified as complaint are escalated — none are auto-posted.
- **SC-004**: Human escalation notifications are delivered within one poll cycle (≤ 60 seconds by default) of the issue being detected.
- **SC-005**: All auto-generated answers are grounded exclusively in configured documentation files; zero unsourced answers are posted.
- **SC-006**: The advocate node processes up to 20 new issues per poll cycle within 10 seconds of additional latency.
- **SC-007**: Setting `advocate.enabled: false` in the config completely disables the feature with no effect on poll cycle duration.

---

## Clarifications

### Session 2026-02-23

- Q: How should the advocate agent read documentation source files? → A: Via GitHub Contents API using the existing GitHub service (`github.py`), fetching each configured file by path and branch ref on every poll cycle. No local filesystem access is required.
- Q: How should the confidence score be computed? → A: Via a pluggable multi-LLM scoring interface. Each configured provider returns `{"confidence": 0.0–1.0, "reasoning": "..."}` in a structured call; the final score is the mean across all providers. V1 ships with Claude only; additional providers (OpenAI, GitHub Copilot) are addable via `scoring_models` config without code changes. Rationale: consensus scoring reduces hallucination risk for customer-facing responses; the abstraction cost is low now but high to retrofit later.
- Q: How should the advocate identify which issues to process and prevent overlap with the board workflow? → A: Advocate-first label approach — the advocate immediately applies `advocate-handled` (for auto-replies/acks) or `needs-human` (for escalations) before posting any comment. The board workflow skips issues with either label. Both label names are configurable. In-memory history provides a secondary within-session deduplication guard.

---

## Assumptions

- The coordinare is connected to a single GitHub repository; multi-repo support is out of scope for this spec.
- Documentation sources are plain-text or Markdown files within the repository, fetched via the GitHub Contents API; binary formats (PDF, DOCX) are out of scope.
- The in-memory response history resets on coordinare restart; cross-session persistence is handled by spec 003 (state persistence) and is out of scope here.
- The advocate node uses the existing Claude service (`claude.py` from spec 001) as the V1 scoring provider; no new API credentials are required for V1. Additional LLM provider adapters (OpenAI, GitHub Copilot) are out of scope for this spec but the `ScoringProvider` interface is designed to accommodate them.
- In V1, with `scoring_models: ["claude"]`, issue classification and response generation are handled in a single Claude API call per issue to minimise latency. When multiple providers are configured in future, provider calls MAY run concurrently (parallel scoring) before aggregation.
- The coordinare's GitHub token must have `issues: write` scope to post comments; this is assumed granted (same token used by spec 001).
- Sensitive keyword matching is case-insensitive substring match against issue title and body; NLP-based sentiment analysis is out of scope.
- The feature-request acknowledgement and off-topic redirect templates are configured by the operator as static strings with no dynamic personalisation in MVP.
- The coordinare's GitHub token must have `issues: write` and `labels: write` scope to apply `advocate-handled` / `needs-human` labels and post comments.
- The `advocate-handled` and `needs-human` labels must be pre-created in the GitHub repository before the advocate is enabled; the coordinare SHOULD create them automatically on startup if absent.
