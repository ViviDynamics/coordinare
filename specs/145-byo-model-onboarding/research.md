# Phase 0 Research: BYO-Model Onboarding

**Feature**: 145-byo-model-onboarding | **Date**: 2026-08-28

Decisions made while implementing. D1 and D5 changed what shipped.

---

## D1: The de-Spark scope was larger than the scrub said (**and the scrub was wrong**)

**Finding**: spec 142's pre-public scrub recorded "Private RFC-1918 addresses outside `specs/`:
**0** — Clean." That was false. The evidence was a `git grep -ilE` pattern using `\b` word
boundaries, and `git grep -E` uses POSIX ERE, which has **no `\b`**. The expression matched
nothing and the zero was recorded as a clean result.

Re-run without `\b`: **12** tracked non-spec files contained `192.168.3.30`, the internal model
host, including `routing.example.yaml`, which users copy.

**Decision**: correct the scrub record (done, separate commit), and replace the hand-run grep with
an automated guard so the result stops depending on anyone writing a portable regex.

**The generalisable part**: a check that silently matches nothing is indistinguishable from a check
that passes. `test_guard_fails_when_an_internal_reference_is_reintroduced` exists solely so this
guard's passing results mean something. That test is not ceremony; it is the difference between
this guard and the one it replaces.

---

## D2: Which values are internal, and which merely look it

**Decision**: the guard targets three **specific known values** rather than the general shape of a
private address:

| Value | Files before | Kind |
|---|---|---|
| `192.168.3.30` | 12 | the internal model host |
| `litellm.vividynamics.com` | 8 | the internal gateway |
| `spark/` | 48 | internal model-identifier prefix |

**Deliberately NOT flagged**: `10.20.30.40` (a documentation placeholder), and `192.168.1.50` /
`192.168.1.10` / `10.0.0.5` (spec-144 tests demonstrating a non-loopback bind). These are
legitimate and a guard that flagged them would be weakened rather than obeyed, which is how
guards die.

**Replacements** use RFC-reserved documentation values so a reader never has to wonder whether an
address is real: `192.0.2.10` (RFC 5737 TEST-NET-1), `.example` (RFC 2606), and `localhost:11434`
where a reader can genuinely use it, since that is Ollama's real default.

---

## D3: `specs/` is excluded, and that is a decision rather than an oversight

**Decision**: the guard skips `specs/`. Those documents were true when written; rewriting them to
remove references would falsify the project's own record of its own decisions.

**Consequence, recorded rather than resolved**: 660 of 1,999 spec files carry `spark/*` and 158
carry the internal gateway hostname. Whether that tree is published at all is a separate,
still-unmade decision on spec 142's pre-public scrub. This feature does not make it.

---

## D4: Presets validate against the real validator, not a copy

**Decision**: `test_preset_validates_against_the_real_validator` constructs an actual
`ProjectConfiguration`.

**Rationale**: it caught three real errors that a schema copy or eyeballing would have shipped:
`agent_transport: docker` is not a valid value (`subprocess` / `ssh` / `kubernetes`); a
`github_token` field is required and was missing; and `kind: openai` **rejects** `base_url`
because that kind is reserved for the native OpenAI API. The generic OpenAI-compatible kind is
`vllm`, which is not vLLM-specific despite the name, and the preset says so because the name
misleads.

---

## D5: Onboarding files must name commands that exist (**changed what shipped**)

**Finding**: the first draft of the presets told the reader to run `bin/coordinare validate` and
`bin/coordinare doctor`. **Neither existed.** There is no `bin/coordinare` script at all; the entry
point is `python -m coordinare`, the validate subcommand is `config validate`, and `--config` is a
**global** flag that must precede the subcommand, so even the shape was wrong.

**Decision**: assert the invocation against the real argument parser
(`test_quickstart_names_the_commands_that_actually_exist`), so an onboarding file naming a
non-existent command cannot ship.

**Why this matters more here than elsewhere**: this spec exists because the first thirty minutes
fail for outsiders. An instruction that does not work is not a documentation defect in that
context, it is the exact failure the feature is meant to remove.

---

## D6: What preflight can and cannot prove

**Decision**: `doctor` checks endpoint reachability, model presence, and the dashboard bind. It
reports plainly that a green result **does not** prove a model produces good output, only that it
exists and answers.

**The model check lists what the endpoint does serve** on a mismatch, because that listing is
almost always the fix: the name is misspelled or the model was never pulled. Demonstrated live
against a real Ollama instance, where it correctly reported the configured model absent and named
the two that were present.

**`CheckResult` refuses to be constructed as a failure without a fix.** A check reporting that
something is wrong without saying what to change has moved the problem rather than found it, so
the invariant is enforced in the type rather than left to review.

---

## D7: A shipped default was leaking our domain (**found during the sweep**)

**Finding**: `src/coordinare/config.py` defaulted `smtp_sender` to `coordinare@vividynamics.com`,
and `services/email.py` repeated it. Every self-hoster enabling email notifications would send
mail claiming to be from **our** domain, which also fails SPF/DKIM and looks like spoofing.

**Decision**: default to `coordinare@localhost`, with a comment explaining why a real domain must
never be a shipped default.

This was not in the issue's scope list. It was found by sweeping for one internal reference and
noticing a different, worse one, which is an argument for doing these sweeps by reading rather
than only by pattern.
