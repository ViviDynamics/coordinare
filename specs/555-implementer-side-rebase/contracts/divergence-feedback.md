# Contract: divergence feedback entry

Channel: `state["relay_feedback"]`, the same list review and QA feedback use. One entry per request:

```json
{"body": "**Branch out of date with main** ..."}
```

The body MUST:

1. name the branch and the main revision (short SHA) the daemon saw;
2. say whether the platform reported a conflict or only "behind";
3. instruct: fetch main, merge `origin/main` into the branch, resolve every conflict keeping the intent of both sides, run the project's checks, commit;
4. forbid rewriting published history (`git rebase`, `git push --force`), with the reason (the push path never force-pushes);
5. say the rest of the turn's work continues after the merge.

The body MUST NOT contain a token, a clone URL with credentials, or file contents.

The entry is idempotent per `(main_sha, head_sha)`: `request_implementer_rebase` does not add a second identical entry to a list that already holds one.
