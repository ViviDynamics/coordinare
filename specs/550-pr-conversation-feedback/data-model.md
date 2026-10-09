# Data model

`pr_comment_tracking`: per-card dictionary containing `pr_node_id`, `updated_since`, and `versions` mapping comment IDs to accepted body digests. Empty legacy/default state. Hydration and retirement clear other cards' tracking.

Conversation feedback item: `id` is a PR/comment/updated_at/body-digest identity, `source=pr_comment`, `comment_id`, `comment_url`, `author_login`, `body`, `submitted_at`, `state=COMMENTED`. This explicitly identifies conversation provenance rather than pretending GitHub submitted a review. Its existing relay/in-flight transport remains unchanged.

Acceptance atomically commits request payload with tracking state. A request is retained until stage completion using #549's dispatched batch. Edits (including A→B→A) become new requests only when body changes; unchanged-body timestamps do not repeat work. Approval/noise versions are acknowledged without dispatch. Unknown authors cannot grant work or merge authority.
