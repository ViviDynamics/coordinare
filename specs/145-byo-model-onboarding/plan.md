# Implementation Plan: BYO-Model Onboarding

**Branch**: `145-byo-model-onboarding` | **Date**: 2026-08-28 | **Spec**: [spec.md](./spec.md)
**Issue**: [#199](https://github.com/ViviDynamics/coordinare/issues/199) — the last launch gate

## Summary

Make coordinare usable by someone who is not us. Four parts, in dependency order: remove every
internal reference a reader could hit and put a guard in front of it; ship a working preset for
each provider shape; give the operator a preflight that fails loudly at setup instead of halfway
through a card; and write the walkthrough that ties them together.

The guard is the load-bearing piece. Spec 142's scrub already tried to answer "are we clean?" by
hand and got it wrong, so the deliverable is not a one-time sweep but a check that cannot silently
pass.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12).
**Primary Dependencies**: none added. `doctor` uses `httpx`, already a runtime dependency.
**Storage**: N/A.
**Testing**: `tests/unit/test_145_byo_model_onboarding.py`, following the spec-142 and spec-144
precedent.
**Constraints**: no workflow changes; `specs/` excluded from the guard; presets must validate
against the real `ProjectConfiguration`; the dashboard must stay on loopback in every preset,
since it is unauthenticated by design (spec 144).

## Constitution Check

| Principle / Gate | Assessment |
|---|---|
| **I. Code Quality First** | PASS. One new module (`doctor.py`), one new subcommand, three data files. |
| **II. Testing Discipline** | PASS, and central. The guard, the presets, the preflight invariant, and the documented commands are all asserted. The guard additionally proves it can fail. |
| **III. UX Consistency** | PASS. The reader is the user. One vocabulary across presets, quickstart, and `doctor` output; every failure names its fix. |
| **IV. Performance by Design** | N/A for the daemon. `doctor` bounds every probe at 5s, because a setup gate nobody waits for is a setup gate nobody runs. |
| **V. Clarity Before Action** | PASS. Seven decisions recorded in research.md, two of which changed what shipped. |
| **Gates 1-3, 5** | Apply; clean. |
| **Gate 4 (integration)** | Partially: `doctor` was exercised against a real Ollama endpoint rather than only mocked. |
| **Gate 8 (review)** | Structurally unsatisfiable solo, as recorded for 142 and 144. |

**Result: PASS.**

## Project Structure

```text
config.example.ollama.yaml              # NEW  Ollama local
config.example.openai-compatible.yaml   # NEW  vLLM / LM Studio / self-hosted LiteLLM
config.example.anthropic.yaml           # NEW  Anthropic API
docs/quickstart.md                      # NEW  clone -> first card
src/coordinare/doctor.py                 # NEW  preflight checks
src/coordinare/__main__.py               # EDIT doctor subcommand + config loading helper
src/coordinare/config.py                 # EDIT smtp_sender default (was leaking our domain)
src/coordinare/services/email.py         # EDIT same
scripts/pull_models.sh                  # RENAMED from pull_spark_models.sh
tests/unit/test_145_byo_model_onboarding.py   # NEW  guard, presets, preflight, quickstart
+ ~70 files with internal references replaced
```

**Structure Decision**: single project. The de-Spark rewrite touches many files but shallowly:
almost all occurrences were comments or example values, not logic.

## Phase 0: Research

See [research.md](./research.md). Seven decisions; **D1** (the scrub's false clean result) and
**D5** (presets naming commands that do not exist) each changed what shipped.

## Phase 2

Tasks were executed inline rather than written up first, at the user's explicit direction to
prioritise completion. The spec, research, and this plan record the same decisions a tasks.md
would have; what is missing is the per-task checklist, not the reasoning.
