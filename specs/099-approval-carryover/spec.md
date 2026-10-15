# Issue 99: carry human approval across permitted daemon commits

An effective human approval remains valid at its approved SHA. It also remains
valid at a later head only when the complete GitHub compare range proves that
the approved SHA is an ancestor and every intervening commit has an author
login returned by the repository service's self_logins().

Every commit must also belong to a successful stage explicitly permitted to
push after approval. The current permitted stage is documenting, including its
successful side run. Implementing, reviewing, security, QA and closing review
have no such permission. A stage record identifies its exact settled SHA. When
the performer reports head_before, the stage verdict retains it and can identify
that run's complete linear commit range. Unrecorded commits and ambiguous merge
ranges require a new human approval. The current stage and Git committer identity
are insufficient evidence.

Reuse the issue 125 SHA compare transport. Enumerate commits independently of
changed files, validate the ahead status and approved merge base, paginate up to
300 commits, and reject missing, duplicate or incomplete results. An API error,
unknown author, empty daemon identity, human or foreign commit, rewritten history,
or missing successful stage evidence leaves the closer waiting. Approval
withdrawal, actionable feedback and existing merge gates retain their precedence.

A successful decision logs monitor_pr.merge_decision with outcome merge and
reason approval_carried_over_daemon_commits. Failed carry-over retains the existing
approval_predates_head wait reason. Comparison failures log only the error type.

Acceptance tests in tests/unit/test_099_approval_carryover.py cover daemon-only,
human, mixed and unknown authors, permitted-stage evidence, comparison errors,
rewritten ancestry, pagination and incomplete responses. Existing approval-race,
withdrawal, stage-record and documentation-gate suites must remain green.

Deployment requires a daemon restart only. No configuration or credential changes
are needed. The optional stage-verdict head_before field defaults to absent for
older state snapshots. Old records can authorize only their exact settled SHA.
The existing token needs repository Contents read access for the compare API.
