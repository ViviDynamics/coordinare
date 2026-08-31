# Spike findings: Switchyard 0.2.0, run rather than read

Everything below was produced by building `switchyard-server` 0.2.0 and driving turns
through an escalation route against a stub upstream, on 2026-08-30. The harness is in
[`spike/`](./spike/); it needs no model and no network beyond the image build.

## F8 — A judge whose reply does not parse degrades silently to no escalation

**The sharpest finding, and the one that decides this.**

The first run of the spike served the weak model on every turn, with the latch never
firing, while the judge returned an escalate verdict every time. HTTP 200 throughout. The
cause was in the server log, not the response:

```
WARN libsy: judge verdict unavailable; routing without one
     judge_model="judge-model" reason="parse_error"
     error=judge reply did not parse as ...::EscalationVerdict: missing field `escalate`
```

The verdict schema is `{"escalate": true, ...}`. My stub had sent
`{"verdict": "escalate", ...}` — a plausible guess, and wrong. Switchyard discarded it and
routed the turn **as if no judge had run**, returning 200 with a weak reply.

Why this matters here specifically: the whole feature is measured by escalation rate, and
this failure mode produces an escalation rate of zero that looks like a healthy system. It
would be visible only in a WARN line in the sidecar's own stderr. And coordinare's judge
would be a **self-hosted model** — spec 124's finding was that self-hosted models produce
almost-right structured output; that is the precise input that triggers this path.

An adopting integration would have to treat "judge parse failures" as a first-class metric
and alarm, or it would ship a routing feature that had quietly stopped routing.

## F9 — With a well-formed judge, the route behaves exactly as documented

Same harness, corrected verdict, `confirmations = 2`, judge always escalating:

| turn | upstream calls | served |
|---|---|---|
| 1 | weak, judge | **weak** |
| 2 | weak, judge, strong | strong |
| 3 | strong | strong |
| 4 | strong | strong |

This confirms the analysis in the spec's F1, as behaviour rather than as reading:

- **Turn 1's bad work is delivered.** The judge flagged it and the weak reply was served
  anyway, because the streak had not reached `confirmations`. In coordinare a turn is a
  commit, a verdict, or a test run.
- **Every unlatched turn costs a judge call.** The saving is real only where
  `judge_cost < strong_cost − weak_cost`.
- **Turn 2 costs three calls** — weak, judge, and strong — for one answer.
- **The latch is one-way and permanent for the session.** Turns 3 and 4 skip the judge
  entirely and never return to weak, so one rough patch pins the rest of the session to the
  expensive model.

## F10 — Escalation attribution is available, just not where the issue expected

The spec's F2 recorded that upstream's own known-issues list says routing-tier attribution
is missing from `/v1/stats` and `/metrics` for escalation decisions. That is true, and it is
not the whole story: `--routing-log-file` writes a per-request JSONL record that **does**
carry the tier.

```
tier=weak        model=weak-model
tier=classifier  model=judge-model
tier=strong      model=strong-model
```

So "what fraction of turns escalated" is obtainable — by reading that file, or from
LiteLLM's per-model counts underneath. A sample is at
[`spike/routing-sample.jsonl`](./spike/routing-sample.jsonl). This is a design constraint,
not a blocker, and it is better news than the spec's F2 alone suggested.

`session_id` is `null` in every record even though the server logged the header on the
request — upstream known issue 0.2.0 #4. Any per-session analysis has to correlate some
other way.

## F11 — Building the image has two traps, both silent until run time

- `switchyard-server` 0.2.0 needs **rustc ≥ 1.96.1**. The host toolchain here is 1.91.0, so
  `cargo install` fails outright. Building in a container is required regardless of whether
  a sidecar is wanted.
- The obvious runtime base is wrong. `rust:1.96-slim` is trixie-based and the binary links
  **GLIBC 2.38**, which `debian:bookworm-slim` (2.36) does not have. The image builds
  cleanly and then dies on first exec with `version 'GLIBC_2.38' not found`. The runtime
  base must be `debian:trixie-slim`.

The working Dockerfile is in [`spike/Dockerfile`](./spike/Dockerfile).

## What this changes about the recommendation

The spec recommended evaluating before adopting. The spike supports that and sharpens it:

- The mechanism **works** when everything is well-formed (F9), and its cost is now concrete
  rather than assumed.
- The visibility problem is **smaller** than feared (F10).
- The failure mode is **worse** than feared (F8), and it lands exactly where coordinare is
  weakest — structured output from self-hosted models.

The thing worth stealing remains the strategy: judge the completed turn rather than predict
the request, and latch rather than flap. Whether that needs a demo-grade Rust service in the
dispatch path of every performer turn is the question the benchmark should answer — and it
needs a reachable gateway, which this environment does not have (spec F7).

## F12 — Escalation cannot fire unless the client sends a session header

**The most decision-relevant finding here, and it came out of a review disagreement.**

An independent reproduction reported that the latch never fires — ten turns, ten judge
calls, zero strong calls — contradicting F9. Both observations are correct, and the
difference between them is the finding:

| turns | `x-switchyard-session-id` | judge calls | strong calls | latched? |
|---|---|---|---|---|
| 5 | sent | 2 | 4 | yes, from turn 2 |
| 6 | **not sent** | 6 | **0** | never |

Without the header every request is its own session, so the consecutive-escalate streak
resets to zero each turn and can never reach `confirmations`. The judge is still called on
every turn. That is **the full cost of escalation with none of its benefit**, at HTTP 200,
with nothing in the response to say so.

This matters for coordinare more than any other finding here. Performers reach the model
through the shim, and `x-switchyard-session-id` is a Switchyard-specific header the shim has
no reason to send. An integration that simply pointed a mode's endpoint at the sidecar —
which is exactly what issue #204 proposes, and what makes rollback "one catalog line" —
would land in the bottom row of that table: paying for a judge call on every performer turn,
escalating never, and looking healthy throughout.

Any adoption must therefore also thread a stable per-session identity from coordinare through
the shim into that header, and prove the latch fires end to end. That is more integration
surface than "an ordinary endpoints entry", and it should be costed before adoption rather
than discovered after.

## Reconciling the two reproductions

For the record, since one of these was reported as a defect in this write-up:

- **F9 stands.** Re-run with the header: turn 1 weak, turn 2 strong, turns 3-5 strong with
  no judge call. Reproduced twice.
- **F8 stands.** Re-run with a malformed verdict: three turns, three
  `judge verdict unavailable ... reason="parse_error"` WARN lines. The report that the
  failure is *completely* silent did not reproduce; the WARN is on the server's stderr, which
  is `docker logs` for a containerised sidecar.
- **The disagreement was worth more than either finding**, because chasing it produced F12.
