# Evidence recovery implementation plan

> **For agentic workers:** Use superpowers:executing-plans within the existing Task 8 ledger; native implementation remains selected.

**Goal:** Complete immutable evidence delivery without model-generated forward ranges, and bound invalid evidence calls without stack-specific rules.

**Architecture:** Coordinare owns required-page sequencing, strict rereads and a monotonic invalid-evidence-call allowance. Nare owns argument validation and typed dispatch outcomes. Existing admission and execution authority remain unchanged.

**Tech Stack:** Python 3.12, asyncio, published nare dependency, existing identity-bound checkpoints and pytest.

**Spec:** [Approved bootstrap design](nare-bootstrap-design.md), plus the maintainer-approved recovery proposal of 2026-11-15.

## Global constraints

- No package-manager, language, framework or filename heuristics.
- Preserve complete serialized pages, actual delivery receipts, required scope and pinned identities.
- Preserve independent review, frozen obligations, clean consumer, provenance and cleanup gates.
- No dependency vendoring, patched site-packages, additional conversation loop or legacy fallback.
- Never restart a held run or refresh spent allowance through resume or incidental progress.
- Keep all private diagnostics; native002 remains stopped during implementation and offline validation.

## Review focus

- Schema rejection before a caller callback must produce a classified outcome and one charge.
- Compaction followed by legitimate rereads must not spend the invalid-call allowance.
- Dynamic source and observation catalogs must supply their own bounds and recovery operation.
- Batched dispatch and cancellation must preserve spent outcomes; stop before subsequent inference.
- Exhausted checkpoints with internally working sessions must perform zero inference on resume.

## Task 8 recovery steps

### 1. Evidence interface

Files: `agent_runtime/evidence.py`, `environment_agent.py`, `test_376_agent_evidence.py`, `test_376_environment_review.py`, `test_376_bootstrap_phases.py`.

- [x] RED: actual native dispatch rejects page arguments to continuation; explicit rereads preserve exact bytes and atomic invalid selection; recovery metadata identifies next operation and current bounds.
- [x] Implement `review_evidence()` / `read_source(path)` / `read_observation(observation_id)` as continuation-only. Add corresponding `reread_evidence(pages)`, `reread_source(path,pages)`, `reread_observation(observation_id,pages)` tools. Reuse `EvidenceStore.read` and complete-page accounting.
- [x] Add page count to bounded envelopes, with safe bounded invalid-selection guidance. Cursor is informational, not an input to continuation.
- [x] GREEN: evidence/source/observation boundary tests, including Unicode, optional sources, compaction and exact payload caps.

### 2. Typed dispatch outcomes

Files: upstream nare caller tools, dispatch and event contracts/tests; Coordinare dependency pin/checkpoint version.

- [x] RED upstream: schema-invalid and caller-classified failures expose safe typed reasons without raw error parsing; unexpected failures remain distinct.
- [x] Implement upstream, test and distribute through normal release provenance; do not patch the installed wheel.
- [x] Pin the verified artifact and refuse incompatible checkpoint policy/runtime identities.

### 3. Bounded recovery

Files: `agent_runtime/nare_runtime.py`, `accounting.py`, `checkpoint.py`, `models.py`, `environment_attempt.py`, `environment_repair.py`, native accounting/checkpoint tests.

- [x] RED: repeated and varying invalid evidence calls, including native argument rejection and interspersed valid reads, stop before another provider request; valid rereads do not count.
- [x] Use an attempt-wide allowance of eight invalid evidence calls, distinct from schema corrections. This is an implementation choice under the approved small bounded allowance, not an increase in existing call/time/repair budgets. Bind the limit and outcome-contract revision into policy identity.
- [x] Persist monotonic consumption and deduplicated outcome receipts; hold with `agent_evidence_call_limit` at exhaustion. Never replenish on new phases, compaction or success. Resume must refuse exhausted or incompatible state before inference.
- [x] Test current-batch dispatch semantics explicitly; do not claim per-call cancellation if upstream delivers outcomes only at the batch boundary.

### 4. Verification and continuation

- [x] Script invalid navigation through real nare dispatch to complete required evidence, then real Coordinare assessment admission; separately prove exhaustion and resume refusal.
- [ ] Run affected 353/376 regressions, unchanged lint ratchet, types, paging benchmarks and exact-head preflight; preserve RED/GREEN evidence in Task 8 ledger.
- [ ] Only after those pass, launch fresh bounded live acceptance under standing authorization. No resurrection of native002 and no readiness claim before required project/consumer/provenance acceptance.

## Review corrections

- [x] Reconcile typed completed outcomes still queued when a native batch is interrupted,
  preserving actual consumption even when the batch exceeds the stopping allowance.
- [x] Refuse domain-held checkpoints independently of native working status; preserve
  the established clean-interruption and provider-failure recovery paths.
- [x] Reject missing or duplicate tool-call IDs before dispatch in both streaming
  and ordinary transports, after recording physical usage.
