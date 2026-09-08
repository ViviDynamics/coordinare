# Spike: does `env_bootstrap` warrant a bounded role workflow?

**Status**: findings, no code. **Written**: 2026-09-08 against `main` `ea76bf6`.

`env_bootstrap` is the last performer role without a workflow. The agreed
ordering was a spike before a spec: decide whether a bounded, code-driven step
sequence is warranted here at all, rather than assuming the 164-173 pattern
generalises to the tenth-and-a-bit role. This is that decision.

**Verdict: warranted, but a thin one.** Scope a workflow *around* the install
turn, not instead of it. The value is a checkpointed verify-repair loop and
step observability. It is not the elimination of model calls, which is what
every prior spec in the programme bought.

---

## What the role is today

Two pieces, in two files, neither of them a workflow.

**1. One unbounded agent turn, steered by roughly 300 lines of prompt.**
`services/http_performer_service.py:1067-1372` assembles the bootstrap persona
from six blocks: `retry_block`, `checklist_block`, `required_sequence`,
`absolute_path_block`, `relocation_block`, then the base persona (with
`verify_block` and `activate_block` selected by whether coordinare wrote those
files). `required_sequence` is a numbered, four-step imperative list that opens
"REQUIRED EXECUTION SEQUENCE. DO NOT SKIP A STEP, DO NOT STOP EARLY."

That is a workflow. It is written in the least verifiable medium available.

**2. A code-driven tail, inline in the status cascade.**
`agent/performer/src/performer/main.py:2761-2890` runs three ordered steps
after the turn: service inference (with its own 600 s ceiling), the spec-101
service-readiness gate, then `verify.sh`. It has no step boundaries, no
durations, no metrics, and no step-level tests. Spec 107 exists solely because
those three were in the wrong order, and the comment block explaining the fix
is nine lines long.

Every other role in this programme moved exactly this kind of tail into named
workflow steps.

## The case for

**The repair trajectory already points here.** Thirteen specs have touched this
role: 060, 063, 073, 076, 077, 087, 088, 091, 092, 093, 101, 107, 116. The
dominant pattern across them is taking authorship away from the model.
Coordinare now owns the dependency manifest and `verify.sh` (077), `activate.sh`
(087), and the service scripts (091, 093). The prompt records why, in its own
comments: a forgetful model fumbled the `.rbenv`-versus-`rbenv` dot prefix
"every run", and a sourced `exit 1` in an agent-written `activate.sh` "knocked
out CLI installs and deadlocked bootstraps". The endpoint of that trajectory is
a workflow. The programme has simply arrived at the role last.

**The failure economics are the worst in the system.** The one bootstrap in
`coordinare.log` ran 15 m 24 s (dispatched 2026-09-06T03:10:07Z, complete
03:25:31Z). Ceilings around it: `bootstrap_max_seconds` 3600,
`bootstrap_idle_timeout_seconds` 600, `env_bootstrap_max_attempts` 3 per spec
SHA before the breaker trips. `serialize_env_bootstrap` can hold every card in
the symphony for the duration. And the whole install is one turn, so nothing is
checkpointed: attempt 2 redoes everything attempt 1 got right. A blind retry of
a quarter-hour job is the most expensive retry coordinare performs, and it is
currently the only kind available.

**The one thing the prompt cannot enforce is the one thing that matters.** The
persona tells the agent to run `verify.sh` itself and "DO NOT end your turn,
and DO NOT report success, until verify.sh exits 0". The 092 comment above
`required_sequence` says in as many words that this is the instruction a
forgetful model ignores: it does the cheap setup, declares done, and never runs
the expensive install. Asking a model not to stop early is not a gate. Code
running `verify.sh` between turns is.

## The case against, which is real

**The install is irreducibly open-ended.** The other ten workflows replace
model *judgement* with code. Here the model is doing *actuation* against an
arbitrary repository and an arbitrary toolchain. No step sequence can enumerate
`rbenv install` versus `nvm install` versus `asdf` versus a bare `pip`. Any
workflow here keeps at least one full agent turn, so the model-call reduction
that justified 172 ("mostly needs no model") is not available.

**The usual new-role hazard is already handled, and so is the usual win.**
Handoff trap 1 does not bite: `env_bootstrap_complete` is already in the closed
`PerformerStatusType` literal (`protocol.py:42`) and the role already returns
before the implementer tail (`main.py:2761`, `main.py:2886`). So there is no
silent-PR risk to fix, and none of the "give the role a terminal status" work
that several earlier specs spent effort on.

**It is a third dispatch path.** `card_context["workflow"]` is set in exactly
one place, `graph/nodes/dispatch_performer.py:1630-1633`. `env_bootstrap` does
not go through the graph node; it has its own `_build_bootstrap_job_payload`,
which never sets it. Threading is cheap (metadata flows into the Score via
`main.py:3424` `**payload.metadata`, and `Score.workflow` already exists at
`models.py:149`), but it is a third path to keep in step with the other two,
and the spec-173 card-less path is only days old.

## Recommended scope, if a spec follows

1. **Keep one agent turn for the install**, via `toolkit.run_agent_turn`, with
   the current persona as its brief. Do not attempt to replace the toolchain
   prose with code.
2. **Move the existing tail into named steps** (inference, readiness, verify,
   report) out of the `handle_status` cascade, so it gains the durations,
   metrics and step tests every other role has. This is the low-risk half and
   is worth doing even if step 3 is rejected.
3. **Add a bounded verify-repair loop.** Code runs `verify.sh`, and on a
   non-zero exit feeds its FAIL lines into a repair turn, up to a small step
   ceiling. This is the move spec 167 made for the implementer. It converts a
   3-attempt whole-job breaker into per-step retries that do not discard a
   successful 15-minute install.
4. **Thread `workflow` through the bootstrap dispatch** as a named change with
   its own test, not as a drive-by.

## Limits of this spike

The case rests on the spec history and on the prompt's own recorded failure
modes, not on measured failure rates. `coordinare.log` holds exactly one
bootstrap and it succeeded first try; live `env_cache` state shows
`bootstrap_attempts: 0` for the single configured symphony (`website`). If the
operator wants the repair loop justified empirically rather than historically,
the cheap experiment is to instrument the existing turn for whether it ran
`verify.sh` at all before reporting done, and collect a handful of real
bootstraps first. That is a smaller spike than this one, and it would settle
step 3 specifically. Steps 1, 2 and 4 do not depend on it.


## Implementation decision after the offline probe

The supplied spike is retained as historical reasoning. The 2026-09-08 offline probe
adds evidence that git change reports miss cache writes and a rewritten verifier can
false-pass. Issue #287 therefore includes in-memory artifact fingerprints and rejection
of changed promised scripts along with the thin workflow. Existing tests already cover
readiness-before-verify and the legacy missing-verifier behavior. No real-model success-rate
improvement is claimed. The user subsequently authorized completing the enhancement.
