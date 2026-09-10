# Overnight QA triage — September 10, 2026

Scope: issues opened during the September 9 evening / September 10 overnight QA run that remained open when triage began. Previously closed fixes (#315–#321, #327, #329, #331) are not reopened or duplicated. All implementation work is in a dedicated git worktree.

| Priority | Issue | Disposition |
| --- | --- | --- |
| P1 | #337 | Fix all-blocked fallback hydration: load the focused session before invoking the graph, so aggregate `assessing` / null blueprint defaults cannot overwrite completed work. Preserve notified blocked sessions and their operator-answer polling. Log real blueprint retirement with its trigger. |
| P1 | #326 | Seed the single symphony's sessions on restore; reconcile sessions even without a focused card. For multiple symphonies, retain unassigned sessions in snapshots and claim them only after a successful board read contains their project item IDs. |
| P1 | #335 | Declare watchdog timer/fingerprint and retry state as LangGraph channels; preserve timer/fingerprint and retry counters through snapshots. Add a debug decision event and compiled-graph regression. |
| P1 | #333 | Apply positive role token budgets to structured workflow steps, including the architect blueprint. Bound retries by the same configured cap and report it accurately. A zero/unset role value retains bounded workflow defaults. |
| P2 | #334 | Post token-cap notifications using `GitHubService.add_comment(issue_node_id, body)`; test against the real service's autospecced contract. |
| P2 | #325 | Fix the independent live-test measurement: bracket two restricted attempts with successful unrestricted controls, distinguish connection failure from pod/attach failure, and treat mixed/missing evidence as inconclusive. Keep production enforcement behavior unchanged. |
| Rollout | #330 | The local deployment already enables assessor, architect, implementer, reviewer, security, QA, documenter, and closer workflows. Document the observed rollout and validation gates below; do not silently change a live deployment from a worktree. |
| Operations | #336 | One confirmed gateway-timeout incident remains for gateway-side diagnosis. The reported 31 occurrences were mostly timestamp matches; see the correction below. No timeout or model-host configuration change is justified by those counts. |

## Blueprint loss: code reproduction and log correlation

The retained daemon log shows all three sessions becoming ineligible after an implementer environment block at 10:40:33 UTC. At 10:42, the daemon takes its all-ineligible fallback. Unlike normal session fanout, that path did not hydrate the focused session into the flat graph state. Its writeback then replaced the saved session with aggregate defaults. At 10:44:11, that card dispatched at `assessing`.

A compiled-graph regression reproduces the loss without a daemon restart: the session contains a completed blueprint, signature, implementing stage and comment watermark; the flat state contains initial defaults; all sessions are blocked. Before the fix, fallback erases the blueprint. After hydration, every field survives. Additional regressions cover notified system blocks. Intentional backward moves still retire a session, now with an explicit audit trigger. Existing dependency-unblock behavior remains covered.

## Gateway count correction (#336)

Counting the literal substring `504` in the retained daemon log from midnight through 09:26:35 UTC yields **31 matching lines**, reproducing the issue comment's count. Counting explicit `504 Gateway Timeout` records yields **two lines**, representing **one incident**:

- 08:53:04.885264 UTC — `performer_endpoint.transition`.
- 08:53:15.221338 UTC — `monitor_performer.terminal_error`.

The other literal matches include timestamp digits, not HTTP status codes. Neither 31 independent failures nor a nine-hour outage is established. Raw logs are intentionally not attached.

Read-only checks later that morning found the LiteLLM liveness endpoint answering normally through Caddy. Spark had approximately 11 GB available RAM, about 1 GB swap used, and low load; recent vLLM samples showed generation progressing with zero queued requests. These observations do not establish conditions at 08:53 or identify which proxy generated the timeout.

Remaining operations work: correlate Caddy and LiteLLM request/error logs for the 08:53 incident with the vLLM request, then compare actual elapsed time and configured timeout limits. Do not increase limits, restart the fleet, or claim a model-memory root cause from the issue's original count.

## Workflow rollout (#330)

The inspected local deployment already has `workflow:` on all eight lifecycle roles. This supersedes the original report that only QA was enabled. A copied configuration passed `python -m coordinare config validate` in the fix worktree with its environment loaded. Their configured contracts are deliberate: assessor emits an assessment; architect emits a blueprint; implementer executes its brief; reviewer/security emit findings; QA emits validation; documenter handles documentation; closer applies the close gate.

For the next deployment, validate a config copied into the deployment worktree, then exercise the existing assessor → architect → implementer path on one QA card. Confirm the blueprint survives an environment hold and restart, the watchdog trips on a frozen turn, and configured token limits reach workflow calls. Then validate reviewer/security/QA/documenter/closer contracts before widening concurrency. Keep the known freeform per-role rollback available by removing that role's `workflow:` setting. No live daemon restart or primary-checkout config edit was performed during this fix batch.

## Verification

- Full coordinare suite, including integration tests, plus focused final regressions.
- Actual daemon startup into a compiled board graph: six unfocused restored sessions retain stages and comment watermarks; one pipeline admitted.
- Compiled watchdog graph, snapshot serialization, and real GitHub service method contract.
- Production toolkit construction for all eight lifecycle roles; raised/lowered/unset role budgets and bounded truncation retries.
- Controlled destination shutdown regression for independent egress measurement; revised comparison passed against local kind.
- Live fleet validation of the code changes remains a deployment step, not a result inferred from unit tests.
