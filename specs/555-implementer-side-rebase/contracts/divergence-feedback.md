# Contract: divergence feedback entry

Channel: `state["relay_feedback"]`, the same list review and QA feedback use. One entry per request:

```json
{
  "kind": "branch_divergence",
  "source": "coordinare",
  "base_branch": "main",
  "main_sha": "<40 hex>",
  "expected_head_sha": "<40 hex>",
  "body": "**Branch out of date with main** ..."
}
```

## Performer obligations

1. Honour the entry only for the implementer role and only when both SHAs are present.
2. At workspace setup, if the checked-out head equals `expected_head_sha`, run `git rebase origin/<base_branch>`. On conflict, abort and leave the tree clean for the agent. Never fail setup because of this step.
3. At push, when the remote head is not an ancestor of HEAD: refuse if a rebase is in progress or the tracked tree is dirty; fall back to the ordinary no-force path if `main_sha` is not an ancestor of HEAD; refuse if the remote head is not `expected_head_sha`; otherwise push with `--force-with-lease=<branch>:<expected_head_sha>`.
4. Never force in any other circumstance.

The body MUST:

1. name the branch and the main revision (short SHA) the daemon saw;
2. say whether the platform reported a conflict or only "behind";
3. instruct: check whether the setup rebase already succeeded; otherwise `git rebase origin/<base>`, resolve every conflict keeping the intent of both sides, continue to the end, run the project's checks;
4. forbid pushing and forbid leaving a rebase in progress (the performer publishes under a lease);
5. say the rest of the turn's work continues after the merge.

The body MUST NOT contain a token, a clone URL with credentials, or file contents.

The entry is idempotent per `(main_sha, head_sha)`: `request_implementer_rebase` does not add a second identical entry to a list that already holds one.
