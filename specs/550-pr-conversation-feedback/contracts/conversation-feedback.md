# Conversation feedback contract

## Field Registry

| Field | Meaning |
| --- | --- |
| pr_comment_tracking | Durable accepted revisions and update watermark scoped to one card/PR |
| pr_node_id | Tracking scope; changing PR resets versions and watermark |
| updated_since | Last fully accepted chronological prefix timestamp; fetch overlaps by one second |
| versions | Comment ID to accepted body digest; unchanged-body edits are suppressed |
| updated_at | API last edit timestamp; included in revision identity |
| html_url | API source URL mapped to comment_url |
| source | `pr_comment`, distinct from submitted review and linked issue |
| comment_id | GitHub conversation comment identity |
| comment_url | User-facing source URL |
| id | PR/comment/updated_at/body-digest identity; reverting text is a new revision |
| author_login | Author subjected to existing reviewer authorization |
| body | Original request text |
| submitted_at | Revision timestamp |
| state | COMMENTED routing compatibility; never APPROVED |

Existing relay_feedback transport carries these additive provenance fields; no new performer service endpoint.
