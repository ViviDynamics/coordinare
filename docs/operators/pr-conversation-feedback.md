# Requesting changes through PR conversation comments

While a card is awaiting PR review, a configured human reviewer or trusted bot
can request work in an ordinary conversation comment, for example:
"Please add a regression test for the empty response." Coordinare routes the
request through its feedback classifier to the appropriate performer stage.
The performer receives the original text, author, comment URL, and
`source=pr_comment`. The dashboard activity feed names the author and links to
the comment, including its revision timestamp.

Configure authors through the existing `human_reviewers` and
`trusted_bot_reviewers` lists. Comments from other authors do not dispatch work.
Acknowledgements, approval-like conversation text, and automated status posts
do not dispatch either. Conversation comments cannot authorize merge; use a
submitted GitHub review to approve a PR.

Each conversation request is independent. Two requests from the same author
both reach the performer, and a later submitted approval does not discard
either request. Submitted reviews continue to use the existing latest-review
policy for each author. Requests are processed before a coexisting approval.

Coordinare tracks accepted comment bodies. Repeated polling and edits that leave
the body unchanged do not repeat an accepted request. Changing the body creates
a new request, including reverting to an earlier body after a different version
has been accepted. Acceptance commits the comment identity and version alongside
the feedback batch. Restart recovery retains that batch through the existing
feedback lifecycle.

Each poll completely fetches the PR conversation before processing its updates
in timestamp order. It classifies at most five new comments and spends at most
20 seconds on fetching and classification together. Failed fetches, incomplete
pages, or budget exhaustion leave unread requests eligible for a later poll;
merge waits until conversation polling catches up. The fetch watermark overlaps
by one second so updates at GitHub's timestamp boundaries remain visible.

If inference is unavailable, a deterministic fallback recognizes explicit work
requests such as "please add", "could you fix", or an imperative beginning with
"update". Questions without a work request are not dispatched by this channel.

See [stale review handling](stale-review-handling.md) for an outstanding submitted
Changes Requested review, and [the interaction checks](../../specs/550-pr-conversation-feedback/quickstart.md)
for the request, edit, restart, and approval scenarios.
