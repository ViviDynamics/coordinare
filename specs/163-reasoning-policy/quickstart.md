# Quickstart: Reasoning Policy and Truncation Classification (163)

## The problem in one line

A reasoning model can spend its whole output budget thinking and return nothing usable —
and coordinare calls that *malformed output*, which reads as "this model can't do the job"
when the real cause is "the budget was too small".

## What changes for an operator (US1)

A role that fails this way now reports **truncation**, and says the output budget was
exhausted. Previously only one role got this; now every role does.

The two failures want different actions:

| Report | What to do |
| --- | --- |
| truncated | raise that role's output budget, or shorten the prompt |
| malformed output | look at the format contract, or the model |

If you have ever chased "the model can't produce JSON" and found the model was fine, this
is that bug.

## What does NOT change

Deliberately, so this feature cannot cause a surprise:

- **ENV_BLOCKED routing** is unchanged. It still applies only where it did before.
- **Which failures are retried** does not widen. The retry gate previously fired on "any
  classified shape", which was only ever reachable for one role; it now names the shapes it
  means instead of quietly generalising to all of them.
- No role's model, budget, or configuration changes.

## What changes for a model (US2/US3) — not yet implemented

A model will be able to declare how its reasoning is produced. Two rules matter:

1. **A model with no policy behaves exactly as today.** Absent is the default and produces
   an identical request.
2. **The setting is per model, and one model is measured harmful.** On `spark/glm-5.3-flash`
   — the model every role currently uses — suppressing separate reasoning does not remove
   the thinking, it moves it into the answer, and the answer stops parsing (reproduced
   twice). It ships with **no policy**, and that is recorded as *measured harmful* rather
   than merely unset, so nobody re-enables it on the assumption it was untried.

The same flag is beneficial on `ada/qwen3-14b` and `ada/qwen3-8b`, which return clean
parseable output with reasoning fully suppressed. **The effect does not generalise. Measure
per model before opting one in.**

## How US2 reaches every backend

The CLIs cannot carry the parameter themselves (codex's config writer forbids extra keys;
openclaw's provider schema has no body hook), so the **proxy shim is the injection point**.

Today `routing.yaml` shims five backends and leaves four direct — including the four that
need the policy most: reviewer and security (openclaw), architect and implementer (codex).
US2 routes those four through the shim too, making it uniform.

That exception turns out to be **stale inside its own spec**. The `routing.yaml` note says
openclaw was moved direct because the shim hop 404'd its OpenAI-wire request. Spec 122 then
fixed exactly that: the shim's front door registers the un-prefixed `/chat/completions` that
a bare-root CLI posts to, and its comment names *"the shared cause of the openclaw +
opencode shim failures"*. `opencode` was switched back; openclaw never was. It runs through
the shim in production today, which is the same fix working on the same failure mode.

So routing openclaw and codex through the shim retires a workaround that outlived its cause
by one commit, and gives one injection mechanism instead of one per backend.

## Gotcha

Do not "just ungate" the classifier. Both of its call sites derive behaviour from the
result — one drives ENV_BLOCKED, the other drives retryability — so widening the classifier
without scoping those two silently rewrites operator-facing behaviour. See plan.md.
