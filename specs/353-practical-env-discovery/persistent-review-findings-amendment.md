# Amendment: persistent coverage findings and explicit capability decisions

## Status and approved intent

Draft for written-spec approval. The user approved the proposed direction with “Okay”: retain unresolved findings across revisions, require evidence-backed resolutions, state capability limitations, preserve mandatory independent review and deterministic clean execution, then rerun Elm using the same model and 60-minute experimental budget. That approval permits this specification; implementation and its implementation plan remain gated.

This architectural amendment extends [spec.md](spec.md), [review-evidence-amendment.md](review-evidence-amendment.md) and [execution-source-amendment.md](execution-source-amendment.md). Repository spec placement overrides the brainstorming default. It introduces no constitution amendment, ecosystem recipes or consumer-repository changes.

## Observed failure and existing flow

The pinned Elm run `review-citations-elm60m-low` at `4e71edd2` stopped after 735.15 seconds with 2864.967 seconds remaining. It completed ten model calls, three fresh independent reviews, one schema correction and two coverage revisions. Installation and clean verification never ran. All owned resources were confirmed absent and the source snapshot unchanged.

Review 1 identified missing Docker coverage, a ShellCheck version conflict and missing promtool. Discovery added ShellCheck/promtool but did not resolve Docker. Review 2 requested an archive URL correction while calling the Docker/Ruby omissions non-blocking. Review 3 rejected Docker coverage and unexplained Ruby exclusion again. The citation fix worked; the coverage revision allowance was exhausted.

`environment_discovery.py` preserves required manifest obligations through `_ObligationLedger` and `_preserve_obligations`, but forwards each assessment as free-form `untrusted_review_feedback`. Fresh reviewers receive independently fetched source evidence and the current manifest, without a structured record of prior findings. Existing required-ID preservation therefore cannot protect a coverage gap that never became a declared requirement.

## Alternatives and selected approach

1. **Persist structured findings and independently adjudicate their resolutions.** Selected. This directly prevents an objection disappearing between revisions while retaining fresh judgment and bounded work.
2. Increase coverage revisions or the timeout. This can permit additional attempts but does not prevent contradictory judgments or forgotten findings. Limits remain unchanged.
3. Reuse one review conversation throughout discovery. This retains context but weakens fresh independent review and grows conversation state. It is not selected.

## Required design

### 1. Attempt-owned finding ledger

Add a shared, bounded finding ledger to the logical attempt, distinct from the existing manifest obligation ledger. A validated reviewer may propose new findings containing a concise issue description, independently read repository paths, affected manifest IDs when present, and category `coverage`, `capability`, `activation`, `service_lifecycle` or `execution_definition`. Every proposed finding is blocking until independently resolved. Non-blocking observations belong in rationale, not in this ledger.

The daemon assigns stable monotonically allocated IDs; models cannot assign identity, delete entries or replace prior evidence. Each finding records its originating candidate digest, source identity and reviewer evidence hashes. Preserve its original text and subsequent decisions in bounded history. Model prose is untrusted evidence, never executable instruction. No semantic deduplication by string similarity: a reviewer can explicitly identify an existing finding ID rather than create a duplicate. Closing a duplicate requires an independently checked link to the surviving finding.

Use a maximum of 32 findings per attempt, 2 KiB per issue/rationale field and 128 KiB for the serialized ledger including history. Source bodies remain in the existing evidence mechanism, not duplicated into ledger entries. Oversized output receives a schema correction within the existing allowance where it is model-owned; an already valid ledger that cannot grow within its bound holds explicitly. Never silently truncate findings, citations or decision history.

### 2. Discovery responses and independent adjudication

Every revised discovery proposal includes a response for each open finding: finding ID, proposed resolution, cited paths and relevant requirement/check/service IDs. A response can explain a correction, demonstrate that the finding was mistaken, or establish from source evidence that the alleged obligation is outside the established project scope. It cannot itself close a finding. Discovery must read cited files at the pinned commit; missing/unknown IDs or unread citations are corrected through existing schema allowances.

For every candidate, construct a fresh independent reviewer context with the current manifest, complete mandatory source evidence, actual capabilities/probe evidence, all findings and their history, and discovery responses. This is structured review evidence, not the discoverer conversation. Independently fetch and hash the additional cited finding/response sources as well as manifest inputs. Subject all material to existing read, context and deadline bounds; missing or changed evidence holds.

The reviewer must explicitly decide every finding against the current candidate: `open` or `resolved`, with cited reasoning. A previously resolved finding must be rechecked after a candidate change and can reopen. New blocking findings are appended. An omitted finding decision, unknown ID, duplicate decision or `accept` with open/new findings is an invalid assessment and consumes the existing schema allowance. Never interpret silence, an overall accept verdict, or vague “non-blocking” prose as closure.

A false-positive finding can be resolved through independently cited reasoning; this is not permission to waive a genuine requirement. The daemon verifies structure, identity, evidence membership and resolution completeness. The agent owns semantic judgment. A structurally valid `revise` consumes the existing coverage allowance; `indeterminate` holds. Budget reservation, ledger decisions and next external action must be checkpointed in the existing ownership order, with no model request after a failed checkpoint.

### 3. Scope and actual execution capabilities

Keep the requested project environment scope fixed for the attempt. Existing command `scope` selectors describe check applicability; they do not authorize dropping a project prerequisite. When no narrower operator scope already exists, discovery reviews the project environment described by its pinned development/test sources, rather than choosing whichever subset its image happens to support.

For a suspected missing capability, discovery must either add the required commands, activation and service lifecycle declarations within the actual execution contract, or supply source evidence demonstrating that the capability is not required for this requested environment. An independently accepted false-positive/out-of-scope explanation may resolve the finding, but must not remove, narrow or weaken an existing required manifest obligation.

If a required capability cannot be supplied by the configured consumer, hold with an explicit capability diagnostic. Do not silently omit that workflow, redefine success, mount a host Docker socket, start a privileged nested daemon, enable verification networking or grant new permissions. Operator changes to infrastructure or project scope require a separate explicit action and a new identity-bound attempt; this amendment does not automate them.

For Elm, Docker/Compose is evidence to assess, not a hard-coded core rule. The same flow handles any repository tool or service. Availability of a CLI alone does not prove the associated service works. Any required service must have executable lifecycle/health evidence under the existing contract. Ruby or another tool may be unnecessary when a source-defined container workflow supplies it, but that explanation must account for the container runtime itself. The user-facing outcome must distinguish unsupported capabilities from timeout, malformed review and failed execution.

### 4. Acceptance, provenance and persistence

Only a fresh independent `accept` with complete finding adjudication and no open findings may freeze a candidate for execution. Carry a canonical digest of the final finding ledger and decision evidence in a versioned coverage assessment bound to the same manifest digest, pinned source, inventory, capabilities, immutable image/platform and reviewed input hashes. Models never supply trusted provenance fields.

Introduce coverage assessment schema version 2 and attempt journal version 5 for this flow. Preserve the existing manifest schema and canonical digest: findings are review evidence, not executable manifest commands. Update producer, installer/performer, daemon, clean verifier and validation harness contract readers together, retaining the same digest across installer and consumer. Persist the full ledger and consumed/max counters across candidate revisions, executable generations and restart. Rehydrate without renewing calls, reads, deadlines or repair allowances.

Legacy review contracts remain parseable for historical reporting, but do not establish readiness under the new findings policy. Active legacy journals without a ledger cannot silently become empty-ledger attempts: hold for explicit restart after confirmed cleanup. A previously saved accepted assessment lacking the new evidence requires fresh review under the new policy before reuse; failed or incomplete saved execution can never be promoted. Unknown versions, tampered ledger digests, mismatched source identity or malformed history hold closed. Document cache invalidation without deleting unrelated cache data or automatically replaying historical commands.

### 5. Execution and bounded repair remain unchanged

Independent review acceptance permits execution; it is not readiness. The accepted manifest must still install successfully and execute every required applicable check and service lifecycle action in the clean consumer. Missing, failed, unknown and unrun results remain blocking. Preserve current source isolation, clean read-only cache verification, artifact/activation checks and image/platform identity guards.

Retain two schema corrections, two coverage revisions and two executable repairs by default, plus existing call/read/context and original aggregate deadline limits. Finding responses do not create another retry category or free model calls. Executable repair retains ledger history, uses confirmed cleanup and a fresh generation, then repeats independent review and all required clean checks. No automatic fresh attempt bypasses exhaustion.

### 6. Secret handling and diagnostics

Reject literal credentials and retain secret-reference/redaction rules for new fields. Validate paths and hashes independently; arbitrary finding/response text must never execute or enter trusted instructions. Private transcripts may retain redacted detailed findings; operator/dashboard summaries expose bounded counts, stable IDs, categories and fixed reason codes rather than raw model rationale or repository text.

Report finding counts/open IDs, category budgets, elapsed/remaining time and stage, with explicit diagnostics for unresolved findings, malformed adjudication, finding/context limits, unsupported required capability and legacy policy invalidation. Preserve the original failed result and cleanup evidence. No dashboard redesign is needed.

## Acceptance scenarios and validation

1. Replay the Elm sequence: review opens a Docker capability finding and two other findings; discovery resolves two but omits Docker. The next reviewer cannot accept by omitting Docker or calling it non-blocking. A complete independently cited resolution or a continued hold is required.
2. A valid false-positive finding can close through exact repository evidence, without deleting required checks. A new required capability remains open if only its CLI version is checked and its required service is unavailable. A genuine unsupported capability produces a clear hold, not successful bootstrap.
3. Missing/duplicate/unknown finding IDs, invented evidence paths, unread citations, fabricated daemon provenance and accept-with-open-findings cannot pass. Corrections consume existing schema allowance. Source/hash drift is terminal, not schema repair.
4. Revisions retain historical findings and required manifest IDs/scopes. A changed candidate invalidates stale closure until fresh adjudication. Executable repair and journal roundtrip preserve history, original expiry and all counters. Checkpoint failure prevents the next external action.
5. Finding/context limits hold without truncation. Legacy pending and accepted state cannot silently acquire current-policy readiness. Corrupt/mismatched ledger evidence is refused by daemon and performer contract validation, including cached verification reuse.
6. Even complete accepted coverage followed by failed, missing or unrun required clean checks remains unready. Existing actual-image source-integrity and artifact-tampering cases remain enabled.
7. Use focused failing-then-passing protocol and restart regressions locally, existing preflight quality gates, broad tests in CI and independent adversarial review of the final diff. No consumer-repository changes or implementation-specific mirrored tests.
8. Rerun pinned Elm first with `onprem/glm-5.3-flash`, low reasoning, the same image-building method and source snapshot, and the approved 3600-second experimental timeout. Inspect each exact proposed command scope before execution. Report actual review/install/clean results and confirm all owned resources absent and snapshot unchanged. A held run is a failure to validate, even if it identifies the limitation correctly. Resume other repositories only after actual Elm success. PR #354 remains unmerged until its existing sample/Elm acceptance and review/CI gates pass.

## Scope and self-review

Work is limited to structured discovery/review envelopes, shared review/journal contracts, their existing acceptance and consumer seams, migration/diagnostics, regression tests and validation documentation. No ecosystem-specific core logic, infrastructure provisioning, automatic scope reduction, new model routing, larger retry allowance or unrelated refactor.

Self-review checked identity ownership, explicit finding closure, false-positive resolution, evidence/context bounds, candidate changes, legacy contracts, durable budgets, secret handling and clean execution. This design prevents forgotten objections; it does not guarantee that the current consumer can support every Elm workflow. Written-spec approval precedes a separately reviewed implementation plan and code changes.
