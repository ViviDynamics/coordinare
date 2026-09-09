# CI infrastructure holds

Enable `persona_scope.env_blocked_gate.enabled: true` on the symphony to hold infrastructure failures before the implementer CI bounce counter. The gate remains default-off. Failure evidence includes Check Run annotations, failed Actions step names, and the workflow's `uses`/`run` boundary. Registry authentication, runner availability, setup-action failures and the same job/error signature on an unrelated head produce an operator-facing cause and action. A repository command failing without that evidence retains the ordinary code-failure path.

The same job/error incident is announced once across cards in a repository. Reminders use `card_blocked_reminder_cooldown_seconds`; clearing one card's marker does not bypass the shared cooldown. The in-memory cache resets at daemon restart. Failed notification sends release their claim so the next cycle can try again.

CI holds stay in `monitoring_performer` with no active dispatch. The daemon checks main and up to ten other open `coordinare/` PRs, refreshing peer evidence at most once a minute. A success for every blocking job on another head must have completed **after** the hold began. It then re-runs each failed Actions job on the held PR once, verifying the current PR head and the job's head before the request. The held PR must still pass its own checks before advancing; another head's green is never a merge pass. Attempted job IDs persist on the session, including ambiguous retry responses, so a restart does not repeat a possibly accepted request. If a retry cannot be confirmed, inspect Actions and rerun manually after resolving permissions. A healthy dependency cache alone never releases a CI hold.

Performer local tests and quality/CI workflows return `env_blocked` for recognized infrastructure errors without spending model repair attempts. Actual runner-reported test failures retain precedence over incidental environment phrases in fixture output. Dispatch/system failures produce **System blocked — operator action required** comments; genuine clarification questions still say **Needs input**.

## Requeue cards blocked before this fix

After repairing the shared infrastructure, use the board to move affected legacy plain-Blocked cards to Todo. The following equivalent operator command requires IDs from your own project's metadata; it does not rewrite snapshots or fabricate a human answer:

```sh
# Find the issue's project item and inspect the board's Status options.
gh issue view ISSUE_NUMBER --repo OWNER/REPO --json projectItems
gh project field-list PROJECT_NUMBER --owner OWNER --format json
gh project item-edit --id ITEM_ID --project-id PROJECT_ID \
  --field-id STATUS_FIELD_ID --single-select-option-id TODO_OPTION_ID
```

Use this only for confirmed infrastructure incidents. Leave cards with unanswered product questions or unaddressed human change requests blocked. For new CI holds, automatic current-head rechecking above is the normal recovery path.
