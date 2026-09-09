# Switchyard evaluation — 2026-09-09

This evaluation does not justify enabling Switchyard in the live deployment or
porting its escalation policy into the native proxy. Keep static GLM routing for
now. The optional integration remains available for controlled experiments;
Switchyard 0.2.0 identifies its server as a demo, not production software.

## Setup and provenance

The retired gpt-oss 20b/120b pair was replaced with the available gateway tiers:
weak `ada/qwen3-8b`, strong and judge `spark/glm-5.3-flash`. Every model request
went through `https://litellm.vividynamics.com`; no model host was probed directly.
The selected performer was Claude Code, using the ordinary endpoint/mode catalog
and the translated shim's per-job `x-switchyard-session-id`. No deployment config
was changed.

The image was `coordinare-performer:150-full`, with the current full entrypoint.
The board comparison used the repaired spec161 real-runner path from PR #303.
Both board runs used the `tiny-multiply` fixture, one repeat, a 600-second budget,
and the same fixed-GLM assignments for every role except implementer. In the
sidecar cell only implementer switched to the escalation endpoint. The final
routing example disables weak-model thinking; the initial diagnostic below did
not. The gateway also served the separate harness experiment during parts of the
board comparison, so wall-clock differences cannot establish isolated throughput.

An interrupted sidecar sample lost its simulated Git listener during operator
cleanup of another experiment. It is excluded, and a fresh sample replaces it.
The earlier malformed standalone reviewer setup and thinking-on assessor timeout
are preparation failures, not the final integration result.

## Initial two-turn diagnostic (thinking enabled)

Each route answered the same two small arithmetic turns correctly. This is a
connectivity/accounting diagnostic, not a coding benchmark or a representative
quality score. It motivated disabling weak-model thinking in the final example.

| Route | Correct answers | End-to-end seconds | Gateway tokens, including judge |
| --- | ---: | ---: | ---: |
| Static Qwen | 2/2 | 26.076 | 1,136 |
| Static GLM | 2/2 | 2.588 | 148 |
| Switchyard escalation | 2/2 | 36.223 | 5,717 |

The two sidecar responses stayed weak (0/2 escalations), but still incurred 4,506
GLM judge tokens in addition to 1,211 weak-model tokens. The client response usage
alone would miss that overhead. The retained gateway `/spend/logs/v2` records
identify the actual Qwen and GLM model groups and match all eight upstream calls.
Their configured self-hosted spend is $0.00; that is not proof of zero operating
cost and cannot support a dollar-savings claim.

## Final board comparison

The whole-lifecycle static run reached implementing after assessment and architecture;
the replacement sidecar run expired in architecture, before the persona under test.
Both are retained as exposure-limit evidence. To actually exercise the routing mode,
`evaluate.py` then ran the same board fixture with an **implementer-only lifecycle**
in both cells. Approval was synthetic and would require the real pytest gate. This
is not a whole-lifecycle quality comparison.

| Isolated implementer cell | Terminal artifact | Pytest gate | Completed within budget | Wall clock including teardown |
| --- | --- | --- | --- | ---: |
| Static GLM | Cancelled at budget | Not reached | 0/1 | 601.169 s |
| Switchyard | Cancelled at budget | Not reached | 0/1 | 601.018 s |

The observed within-budget completion delta is **0 percentage points**. Neither
sample reached the acceptance gate, so this does **not** establish equal code quality
or an acceptance-test pass-rate delta. Completed-job latency and routing's added
latency on this coding task are unknown; the roughly 600-second times are imposed
limits, not successful-task latencies.

The sidecar completed seven streaming CLI requests, all selecting Qwen, with the
same session identity. Its separate startup health request had an empty session ID.
Thus the observed served-strong fraction was **0/7 CLI requests**. There were no
`judge verdict unavailable` or `parse_error` messages in the captured server log.
The usage log includes eight weak calls and eight classifier calls (including the
health probe): 131,760 Qwen tokens plus 25,167 GLM judge tokens, 156,927 logged tokens.
This is completed-upstream-call usage, not a complete accounting of cancelled work.
The benchmark's own usage is missing for these cancelled dispatches; do not read it
as zero cost. Successful-stage zero usage is separately tracked in [#305](https://github.com/ViviDynamics/coordinare/issues/305).

At 03:41:40 the sidecar performer logged a postprocessing push failure caused by
unstaged changes, followed by `job_executor_failed`. That terminal failure was absent
from the final benchmark dispatch, which remained unclassified until cancellation.
The raw artifact is unchanged; [#306](https://github.com/ViviDynamics/coordinare/issues/306)
tracks the lost terminal observation. This coding/workspace failure and observation
gap prevent attributing the non-completion to routing quality.

The earlier standalone assessor smoke completed `assessment_complete` after real
Claude tool use through shim → Switchyard → LiteLLM → Qwen. Along with the isolated
implementer's actual mode-resolved traffic, this validates the transport/session path;
it does not claim that the whole implementer workflow succeeded.

## Reproduction and retained evidence

Merge PR #303 first. Export the gateway key and the local demo's dedicated credential,
set `SWITCHYARD_ROUTES_FILE` to the absolute path of this spec's `live/routes.toml`,
and start the optional compose service. Build the current full performer image, then
run from the repository root:

```sh
PYTHONPATH=src:agent/performer/src .venv/bin/python specs/150-switchyard-routing-sidecar/evaluate.py --output /tmp/switchyard-new-run --seconds 600
```

The output directory must be new. The script holds the model assignments fixed,
changes only implementer's selected mode, and writes no resolved credentials.
[Retained evidence](evidence/2026-09-09/report-provenance.json) includes both pairs of
board artifacts/scores, the initial diagnostic, actual gateway accounting, final
routing usage, session-bearing request logs, and the extracted executor diagnostic.
The optional service was stopped after measurement. No deployment config changed.

## Scope of the conclusion

No isolated GPU utilization, saturated concurrency, or throughput measurement was
performed. The initial diagnostic added strong-model judge work rather than
removing it, and no measured fleet-capacity gain is established. One bounded
coding fixture also cannot establish a stable pass-rate improvement. Do not port
or deploy the policy on this evidence. Reconsider only after a representative,
repeated workload shows a quality-preserving benefit with complete upstream usage
and parse-error accounting.

The operator guide documents the supported session path, dead-sidecar health
failure, dedicated credential, log fields, parse alarms, and one-mode rollback.
Default-off behavior and session isolation are verified in the focused tests.
