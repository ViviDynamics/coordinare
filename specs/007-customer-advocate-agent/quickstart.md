# Quickstart: Customer Advocate Agent (007)

**Feature**: 007-customer-advocate-agent
**Date**: 2026-02-24

---

## Prerequisites

- Coordinare already running with spec 001 configuration
- GitHub token with `issues: write`, `labels: write`, and `contents: read` scopes (same token used by spec 001; ensure `labels: write` is granted)
- `advocate.github_repo` set to the repository name in `config.yaml`

---

## Enable the Advocate

Add the following block to `config.yaml`:

```yaml
advocate:
  enabled: true
  confidence_threshold: 0.70
  doc_sources:
    - README.md
  sensitive_keywords:
    - billing
    - payment
    - security
    - legal
  scoring_models:
    - claude
  support_channel_url: "https://github.com/your-org/your-repo/discussions"
  github_repo: "your-repo"
```

---

## Startup Behaviour

When `advocate.enabled: true`, during `coordinare start`:

1. `GitHubService.ensure_labels_exist()` checks for `advocate-handled` and `needs-human` labels in the repository. Missing labels are created automatically (blue for `advocate-handled`, yellow for `needs-human`).
2. Label node IDs are cached for the session (`label_id_cache`).
3. The `advocate_scan` LangGraph node is active at the head of each poll cycle.

---

## Running

The advocate runs automatically on every poll cycle. To run a single cycle for testing:

```bash
cd src
COORDINARE_OUTPUT_MODE=structured python -m coordinare --max-cycles 1
```

Look for structured log entries with `event: advocate_scan_complete` and `event: advocate_issue_processed`.

---

## Testing

Run the full advocate test suite:

```bash
cd src
pytest tests/unit/graph/nodes/test_advocate.py -v
pytest tests/unit/services/test_scoring.py -v
pytest tests/unit/models/test_advocate_models.py -v
pytest tests/integration/test_advocate_scan.py -v
pytest tests/contract/test_github_advocate_queries.py -v
```

---

## Manual End-to-End Tests

### Test 1: Auto-reply to a documented question (US1)

1. Create a GitHub issue with a question about a topic covered in your `doc_sources` (e.g., "How do I configure the poll interval?").
2. Run one coordinare poll cycle (`--max-cycles 1`).
3. Verify:
   - The issue has the `advocate-handled` label.
   - A comment appears on the issue citing the source document.
   - No second comment appears on subsequent cycles.

### Test 2: Escalation for sensitive keyword (US2)

1. Create a GitHub issue containing the word "billing" in the title or body.
2. Run one coordinare poll cycle.
3. Verify:
   - The issue has the `needs-human` label.
   - A holding comment appears on the issue.
   - A Slack or email notification is sent to the configured reviewer.

### Test 3: Escalation for undocumented topic (US3)

1. Create a GitHub issue asking about a topic NOT covered in any `doc_sources` file.
2. Run one coordinare poll cycle.
3. Verify:
   - The issue is escalated (has `needs-human` label).
   - The structured log contains `reason: no_documentation_match`.

### Test 4: Feature request acknowledgement (US4)

1. Create a GitHub issue that reads like a feature request (e.g., "It would be great if coordinare could support multiple boards").
2. Run one coordinare poll cycle.
3. Verify:
   - The issue has the `advocate-handled` label.
   - A configured acknowledgement comment appears.

### Test 5: Bug report silence (US4)

1. Create a GitHub issue describing a bug.
2. Run one coordinare poll cycle.
3. Verify:
   - The issue has the `advocate-handled` label.
   - No comment is posted by the advocate.
   - The board workflow picks up the issue normally (no label blocks it — bug reports use `advocate-handled` to prevent re-processing, but the board workflow processes issues by project board column, not by label).

---

## Disable the Advocate

```yaml
advocate:
  enabled: false
```

The `advocate_scan` node becomes a no-op pass-through with zero additional latency. No other configuration changes are required.

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `advocate.enabled` | `false` | Master switch — set to `true` to activate |
| `advocate.confidence_threshold` | `0.70` | Minimum score for auto-reply |
| `advocate.doc_sources` | `["README.md"]` | Repository-relative paths to documentation files |
| `advocate.scoring_models` | `["claude"]` | LLM providers for confidence scoring (V1: claude only) |
| `advocate.sensitive_keywords` | (10 keywords per FR-007) | Issue title/body keywords that force escalation |
| `advocate.handled_label` | `"advocate-handled"` | Label applied when advocate takes any action |
| `advocate.escalation_label` | `"needs-human"` | Label applied when escalating to human |
| `advocate.holding_comment_template` | (see spec) | Comment posted on escalation |
| `advocate.acknowledgement_template` | (see spec) | Comment posted for feature requests |
| `advocate.redirect_template` | (see spec) | Comment posted for off-topic issues |
| `advocate.disclosure_template` | (see spec) | Appended to all AI-generated responses |
| `advocate.support_channel_url` | `""` | URL referenced in off-topic redirect |
| `advocate.github_repo` | `""` | Repository name — required when enabled |

---

## Observability

All advocate events emit structured log entries (via `structlog`):

| Event | Fields |
|-------|--------|
| `advocate_scan_start` | `cycle_id`, `open_issues_fetched`, `unprocessed_count` |
| `advocate_issue_processed` | `issue_id`, `issue_number`, `classification`, `action`, `confidence`, `elapsed_ms` |
| `advocate_issue_escalated` | `issue_id`, `issue_number`, `reason`, `notified_channel` |
| `advocate_scan_complete` | `cycle_id`, `issues_processed`, `elapsed_ms` |
| `advocate_doc_fetch_warning` | `file_path`, `ref`, `error` |

Prometheus metrics (appended to existing `metrics.py`):

| Metric | Type | Labels |
|--------|------|--------|
| `coordinare_advocate_issues_processed_total` | Counter | `action` |
| `coordinare_advocate_issues_escalated_total` | Counter | `reason` |
| `coordinare_advocate_scan_duration_seconds` | Histogram | — |
