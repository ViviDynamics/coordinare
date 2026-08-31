# Feature Specification: Switchyard routing sidecar — evaluate before adopting

**Feature Branch**: `150-switchyard-routing-sidecar`
**Created**: 2026-08-30
**Issue**: #204

## Context

Issue #204 proposes integrating [NVIDIA NeMo Switchyard](https://github.com/NVIDIA-NeMo/Switchyard)
as an optional, default-off sidecar in front of LiteLLM, giving coordinare per-request weak/strong
model-tier selection it has nowhere today. The prize is real: spec 077 found that most agent turns
are mechanical tool-following, so routing those to a small model would be genuine capacity
headroom on a self-hosted fleet.

Before writing that integration, this spec establishes what Switchyard actually is at v0.2.0, and
what integrating it would actually cost. Several things differ from the issue's description —
which was written on 2026-08-07 — in ways that change the decision.

## What was verified, and how

Checked against the upstream repository and by building it, on 2026-08-30.

### F1 — The escalation router does not do what the issue says

The issue describes escalation as "run weak first; a judge model reads the response and re-runs on
strong only when needed", framed as **per-request** tier selection. The documented behaviour is a
**one-way session latch**:

- every unlatched turn calls the weak target **and** the judge — a judge call per turn, always;
- an escalate verdict increments a streak; a decline resets it;
- the weak reply is **served** until the streak reaches `confirmations`, so with the documented
  `confirmations = 2` at least one turn the judge already flagged as bad is handed to coordinare;
- once latched, the session goes to strong and the judge is skipped, permanently for that session.

Two consequences for coordinare specifically. Bad work is *delivered* before rescue triggers — and
in coordinare a bad turn is a commit, a verdict, or a test run, not a chat message someone can
ignore. And the "capacity headroom" arithmetic changes: every unlatched turn costs weak + judge,
so the saving is real only where `judge_cost < strong_cost − weak_cost`, which on a self-hosted
fleet is a question about the judge model, not about Switchyard.

### F2 — The observability the issue calls "the whole point" is a known gap

The issue says "what fraction of turns escalated — that number is the whole point". Switchyard's
own `docs/known_issues.md` for 0.2.0, item 2: routing-tier attribution is **missing** from
`GET /v1/stats` and `/metrics` for escalation decisions.

It is recoverable from the other side — LiteLLM sits behind the sidecar and sees the real model
per request, so escalation rate can be derived from per-model request counts. That is a design
constraint to build for, not a blocker, but it is the opposite of what the issue assumed.

### F3 — Tool-bearing requests are a known failure mode

Known issues 0.1.0, item 2: tool-bearing Codex requests may fail when Switchyard routes them to an
upstream that accepts only a fixed set of tool names or schemas. Coordinare has already paid for
this lesson once — the 077 finding that LiteLLM's transform layer mangled tool calls is *why* the
issue puts Switchyard in front of LiteLLM rather than behind it. The same class of defect exists
in the component being added.

### F4 — Maturity is worse than "pre-alpha" implies for the piece we need

Upstream grades its own components: `libsy` Beta, `switchyard-llm-client` Alpha,
`switchyard-runner` Alpha, and **`switchyard-server`: "Demo server, not for production use."**
The sidecar topology needs precisely `switchyard-server`.

### F5 — There is no published image, and it will not build on this toolchain

`switchyard-server` installs via `cargo install`. There is no container image to run as a sidecar,
so we build one. It requires rustc ≥ 1.96.1; the host toolchain here is 1.91.0, so the build must
happen in a container regardless. A working two-stage image is included with this spec.

### F6 — "No new backend kind" does not quite hold

The issue says Switchyard needs no new backend concept, just an `endpoints` entry. `Endpoint.kind`
is a closed `Literal["litellm", "ollama", "vllm", "openai", "anthropic"]` under `extra="forbid"`.
Switchyard would have to be declared `kind: litellm` — which works, because that kind means
"self-hosted, needs `base_url`, proxy-eligible", and is exactly how Switchyard behaves from
coordinare's side — while being untrue as a label. The alternative is one more `Literal` value.
Small either way, but it is not nothing, and the choice should be made deliberately.

### F7 — The acceptance criteria cannot be verified in this environment

Four of the seven require a live path through the sidecar to LiteLLM to a model backend. The
configured gateway (`litellm.vividynamics.com`) is not reachable from here, so an end-to-end
performer run, spend attribution, and the benchmark comparison cannot be demonstrated. Anything
this spec claims about them would be a claim, not a result.

## The decision this spec exists to inform

The issue already anticipates it: *"if escalation routing proves out and Switchyard's maturity
stays a concern, port the winning strategy natively — classifier routing as a LiteLLM pre-call
hook, escalation as a fourth strategy in the 080 proxy — and retire the sidecar."*

F1–F4 make that the live question rather than a footnote. What Switchyard offers is a *strategy
design* worth stealing; what it costs is a demo-grade Rust service in the request path of every
performer turn, carrying a known tool-call failure mode, missing the metric the exercise is
measured by, and requiring us to build and maintain its image.

**Recommendation: evaluate, do not adopt yet.** Specifically:

1. Build the image and stand the sidecar up against a stub upstream — cheap, local, already done
   in part, and it settles whether the escalation route behaves as documented.
2. Measure escalation on the bench substrate *once a real path exists*, not in this environment.
3. Decide adopt-vs-port with those numbers, against spec 142's supply-chain posture: an external
   demo-grade binary in the dispatch path is exactly what that posture is about.

That ordering costs one image build and answers the question the integration was going to bet on.

**The spike then found something that raises the price of adoption** (findings F12): escalation
only works if the client sends `x-switchyard-session-id`. Without it the judge is called on every
turn and the latch never fires — full cost, no benefit, HTTP 200. Coordinare's performers reach
the model through the shim, which has no reason to send a Switchyard-specific header, so an
integration that just points a mode's endpoint at the sidecar lands exactly there. Adoption
therefore also means threading a stable per-session identity from coordinare through the shim,
which is more surface than "an ordinary endpoints entry" and is not in the issue's estimate.

## What issue #204 asked for, and what this branch delivers

Named explicitly, because the gap between the two is the point of this spec and should not
have to be inferred.

| #204 acceptance criterion | Status |
|---|---|
| A mode drives a performer end-to-end through sidecar → LiteLLM → backend | **Not delivered.** Needs a reachable gateway (F7). Also needs session-identity threading nobody had costed (F12). |
| LiteLLM per-model spend reflects the chosen tier | **Not delivered**, same reason. The design reason to expect it holds: LiteLLM sees the real model. |
| Default-off verified with the sidecar unconfigured | **Trivially true and untested** — nothing in coordinare was changed, so there is nothing to be off. |
| A dead sidecar surfaces as a dispatch-time health failure | **Not delivered.** No integration exists to wire the 099 probe into. |
| Escalation-rate visibility documented | **Delivered**, and corrected: not via `/metrics` as assumed, but via `--routing-log-file` (F10). |
| Benchmark comparison vs static assignment, with a keep/port/retire recommendation | **Not delivered.** Needs a reachable gateway. A recommendation is given, on other evidence. |
| Operator doc: compose service, routes.toml, maturity warning | **Partly delivered**: a working image build and a `routes.toml` exist in `spike/`; no operator doc, because documenting an integration this spec recommends against would be premature. |

Four of seven need infrastructure this environment does not have. That is a reason to defer
them, not a reason to pretend they are done.

## Out of scope

- Adopting Switchyard into any default configuration. Nothing here changes coordinare's behaviour.
- The benchmark comparison, which needs a reachable gateway (F7).
- Any change to the dispatch path, the `endpoints` catalog, or the Helm chart.
