# Research decisions

GitHub's issue comments REST endpoint also serves ordinary pull-request conversation comments. Its `since` query filters by last update time, enabling edit detection; overlap is required at timestamp boundaries. Source: https://docs.github.com/en/rest/issues/comments#list-issue-comments.

Existing submitted review routing already carries body/inline feedback to performers. Keep its review-author supersession separate from independent conversation comments. Reuse the existing reviewer authorization and durable feedback lifecycle, with a dedicated polling/classification helper to keep monitor_pr focused.

No work-issue-speckit skill is installed in the available catalog or filesystem. Follow the repository's speckit.specify/plan/tasks/analyze/implement command instructions directly. User's explicit unattended ship-issue invocation supplies implementation authorization; do not insert another approval pause.
