# 080 live-round findings

Issue [#527](https://github.com/ViviDynamics/coordinare/issues/527), task T037.

## Cluster round (2026-10-08 UTC)

The validation used a dedicated [project 19](https://github.com/orgs/ViviDynamics/projects/19)
and temporary `coordinare-t037` daemon in the cluster cluster's `coordinare`
namespace. A second dedicated [project 20](https://github.com/orgs/ViviDynamics/projects/20)
and `coordinare-t037-codex` daemon exercise the Codex Responses wire with
[sample card #20](https://github.com/ViviDynamics/coordinare-e2e-sample/issues/20).
The existing `coordinare-0` daemon and project 16 are unchanged.
Both daemon and performer use release `2026.10.12` (coordinare commit
`a9deacbdb53ab098baa005797e4da8184664c1dc`).

The real sample [card #19](https://github.com/ViviDynamics/coordinare-e2e-sample/issues/19)
asks the implementer to add `collapse_whitespace` and tests. Its model catalogs
resolve `always-glm-flash` to `spark/glm-5.3-flash` for both thinking and tool
legs, through the in-cluster LiteLLM gateway. This measures two sequential
planner/executor calls on one model; it does not compare different weights.
`on_think_error: fail` prevents planner failures from silently falling back.

The later retries use `ada/qwen3-8b` for thinking, with its supported
`disable_thinking` policy, and `spark/glm-5.3-flash` for tool execution.
The policy activates generation forwarding for both legs. The final Claude
Code retry requests a 2,048-token output limit. This policy is applied to
Qwen only; GLM's harmful-policy guard is preserved.

### Measurement method

The current-release Claude Code backend enables `--include-partial-messages`
and tees stream-json stdout when `LITELLM_PROXY_CAPTURE_DIR` is configured.
A temporary `sitecustomize.py`, mounted only in this validation performer,
observes `SseStreamWriter` calls. It records monotonic time from writer creation
to first emitted block and completion, upstream/text fragment counts, plan
length and tool-call count. It compares actual SSE blocks with the buffered
renderer for the same accumulated response, coalescing adjacent text/thinking
fragments while preserving indices, tool inputs and terminal events. These
timings include planning and upstream latency; they do not measure network
arrival at the CLI. CLI partial events separately establish receipt.

Only sanitized metrics and selected lifecycle facts are published. Raw CLI
output and capture artifacts remain local. No production source is modified
for instrumentation.

For Responses, normalization also ignores sequence numbers and coalesces
adjacent output-text deltas. Those fields vary with fragment boundaries;
output items, tool inputs and completion envelopes remain in the comparison.

### Completed requests in the final Claude Code retry

These are proxy emission timings, including the sequential planner call.
An empty plan has no surfaced planning block; its first block starts the
executor response. [Sanitized records](evidence/cluster-claude-metrics.jsonl)
retain millisecond values and SSE block counts.

| End UTC | First block (s) | Total (s) | Upstream / text deltas | Plan chars | Tools | Parity |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 03:15:21 | 71.206 | 77.222 | 67 / 6 | 0 | 2 | mismatch (#538) |
| 03:16:35 | 69.984 | 74.341 | 48 / 5 | 396 | 3 | match |
| 03:18:12 | 66.707 | 95.929 | 299 / 28 | 1533 | 2 | match |
| 03:19:26 | 59.369 | 74.487 | 157 / 11 | 1138 | 1 | match |
| 03:20:28 | 58.486 | 61.269 | 31 / 6 | 1841 | 2 | match |
| 03:21:36 | 51.894 | 66.081 | 146 / 13 | 751 | 1 | match |
| 03:22:26 | 37.392 | 49.752 | 127 / 14 | 566 | 1 | match |
| 03:22:57 | 24.599 | 30.837 | 67 / 8 | 383 | 1 | match |
| 03:24:29 | 79.155 | 86.868 | 81 / 14 | 1756 | 2 | match |
| 03:25:24 | 35.299 | 53.535 | 190 / 10 | 339 | 1 | match |
| 03:25:56 | 26.763 | 32.854 | 65 / 9 | 260 | 1 | match |
| 03:26:54 | 27.476 | 57.136 | 308 / 146 | 330 | 0 | match |

### Per-backend verdicts

| Backend / role | Verdict | Evidence / follow-up |
| --- | --- | --- |
| Claude Code / implementer | Stage completed; needs-fix | Final run succeeded and handed off to review; streaming/tool contract observed, one parity mismatch (#538); earlier auth and cleanup failures below |
| Codex / implementer | Needs-fix | Real `/responses` requests and three tool executions; independently reproduced text-frame/index defect, #537 |
| OpenCode | Not exercised | Not configured on the cluster deployment in this round |
| Junie | Not exercised | Not configured on the cluster deployment in this round |
| Pi | Not exercised | Not configured on the cluster deployment in this round |
| OpenClaw | Not exercised | Not configured on the cluster deployment in this round |
| Hermes | Not exercised | Not configured on the cluster deployment in this round |

This is the T037 one-stage validation plus a Codex investigation, not a full
seven-backend certification. A backend is not marked contract-respecting
merely because its unit tests pass or another backend shares its wire format.

### Auth propagation regression: needs-fix

The initial real Claude Code dispatch failed before model execution: the
proxy returned three 502 responses and the CLI's retry watchdog terminated
the stage. A second run instrumented the underlying exception and recorded
three upstream 401 responses at 02:48:52, 02:48:53 and 02:48:54 UTC, each with
`auth_present=false`.

The daemon's own inference request succeeded using `LITELLM_MASTER_KEY`.
Its dispatch forwarded the endpoint credential as `ANTHROPIC_AUTH_TOKEN`,
but the orchestration refs still named `LITELLM_MASTER_KEY`. `build_upstream`
therefore found no token in the performer environment. A gateway probe
returned 401 without authentication and 200 with the existing credential.

Filed [#534](https://github.com/ViviDynamics/coordinare/issues/534), including
the reproduction and requested coverage for separate tool/thinking/classifier
credentials. The validation retry explicitly forwards
`performer_endpoints[].env.LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}`. This is
an operator workaround; this findings PR does not fix generic auth dispatch.

### Namespace-wide startup cleanup: needs-fix

The first authenticated Claude Code run completed three streamed requests
and five tool executions. While its fourth request was still running,
starting the second temporary daemon deleted its performer. The second
daemon logged `kubernetes_runtime.swept count=1` at 02:59:29.935904 UTC,
using a selector for every managed performer in the namespace. The first
daemon had successfully polled its running job at 02:59:04 UTC, then reported
transport failures after deletion. Its capture has no terminal CLI result.

This is an interrupted stage, not a successful Claude Code stage or proof
of a model timeout. Starting the second daemon was an operator action in this
validation. The cleanup caller nevertheless treated another daemon's live
performer as a crash orphan. Filed [#535](https://github.com/ViviDynamics/coordinare/issues/535)
with the selector, timestamps and ownership analysis. Subsequent validation
runs use one temporary daemon at a time.

### Generation limits: needs-fix

The default canonical upstream renderer omits CLI generation limits unless
generation preservation is active. Executing `_render_body` inside the
deployed performer with `generation={"max_tokens": 2048}` yielded no upstream
`max_tokens`; setting `preserve_generation=True` yielded 2048. Filed
[#536](https://github.com/ViviDynamics/coordinare/issues/536). The long live
requests motivated this check, but do not establish that missing limits
were their only cause. Codex's observed planner requests did not supply an
explicit limit, so preservation alone cannot add one.

### Responses text-frame/index regression: needs-fix

An independent reproduction using the unmodified renderer, without the
observer, with `expose_plan_as="thinking"` and final `reasoning="plan"`,
exposed missing message-opening frames. `start("plan")`, one text
delta `"hello"`, and `finish` produce only a reasoning item at output index
0, then text deltas/done at that same index, with no message-item or
content-part added event. The final response places the message at index 1.
The buffered renderer correctly emits the message-opening events at index 1.

`SseStreamWriter.delta` sets `_text_streamed=True` before calling
`_responses_text_delta`, whose first-fragment guard therefore never opens
the message. Filed [#537](https://github.com/ViviDynamics/coordinare/issues/537)
with the deterministic reproduction and required cases. The Codex run
accepted tool responses and executed tools; its full stage and text-stream
parity are not certified.

The initial Responses observer also contained a separate string-vs-object
delta parsing bug, causing errors after terminal frames were emitted. That
instrumented run is excluded from timing/parity claims. The observer was
corrected and checked against all three wire formats: the tested nonempty-plan
Anthropic/OpenAI fragmented-text fixtures match buffered rendering, while Responses correctly
reports the independently reproduced production mismatch. No production
renderer fix is included in this validation PR.

### Empty-plan reasoning parity: needs-fix

The final Claude Code retry's first request completed at 03:15:21 UTC with
an empty planner result and failed the semantic parity comparison. The
following request, with a 396-character plan, passed. An independent fixture
with the observer disabled confirms that `start("")`, streamed `"hello"`,
and a final response containing executor reasoning produce only a text block
in the stream, but thinking plus text in the buffered renderer.

`strategies.act` preserves executor reasoning via `plan or resp.reasoning`,
while the writer starts with the empty plan and drops upstream reasoning
fragments. Filed [#538](https://github.com/ViviDynamics/coordinare/issues/538)
for consistent empty-plan semantics and coverage across wire/exposure modes.
This does not establish that the mismatch caused a CLI failure.

### Completed stage and hand-off

The final run started at 03:14:00.819825 UTC. The Claude Code CLI returned
`subtype=success`, `is_error=false`, `num_turns=18`. At 03:27:22.896513 UTC
the daemon recorded `terminal_state=succeeded`, then persisted the PR
artifacts at 03:27:24.435414 UTC. The resulting
[sample PR #21](https://github.com/ViviDynamics/coordinare-e2e-sample/pull/21)
has head `aaf07124c04c88096e651759712de46a9e986375`.
A fresh project-19 read-back confirmed the card's **In Review** status and
linked PR; the daemon requested human review at 03:27:26.267627 UTC.

The implementer CI gate logged `ci_gate.no_workspace_path` for this HTTP
performer; it did not run workspace-based evaluation. Independently, the
sample PR's `ci` check completed SUCCESS at 03:27:15 UTC on the same head.
The card is left for human review; this round does not claim review or merge.

The [sanitized CLI summary](evidence/cluster-claude-cli-summary.json) records
12 message starts/stops, 40 content-block starts/stops, 298 content-block
deltas, 17 tool calls and 17 matching tool results. These include Read, Edit,
Bash, pytest and ruff. Initial pytest collection failed until the package was
installed, and ruff found an import-order issue; the agent recovered. Final
commands returned **23 tests passed** and **All checks passed!** respectively.
Successful CLI tool results plus subsequent model requests establish the
round trip, rather than merely counting generated tool calls.

Across 12 completed proxy requests, there were 1,586 upstream fragments and
270 text fragments. Eleven requests matched buffered rendering; the one
empty-plan mismatch is #538. Server-side text fragments and CLI content-block
deltas differ because the latter also include surfaced plans and tool inputs.

Both dedicated boards are archived, the abandoned Codex sample issue #20 is
closed as not planned, and sample issue #19 / PR #21 remain available for
inspection. Temporary validation daemons, config maps, and task cache
directories are removed. The original `coordinare-0` remains running.

Repository validation before pushing these documentation changes:
`make test-all` — **9,514 passed, 56 skipped, 107 deselected**. No production
source, test, or installed skill is changed by the findings PR.
