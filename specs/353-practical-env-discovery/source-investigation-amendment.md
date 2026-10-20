# Explicit investigation before source-scope revision

Issue: #353. PR: #354. Status: proposed written amendment; not approved for implementation.

## Intended outcome and observed failure

Complete Elm bootstrap with independently accepted source coverage and executable
coverage, followed by every required deterministic clean-consumer check. Preserve
technology agnosticism, immutable provenance, bounded repair and failed-check holds.
This amendment makes the already approved direction, "investigate outstanding source
references before resubmitting," enforceable rather than relying on prompt compliance.

The fresh pre-merge Elm experiment on Coordinare `ea4f4c33825cae8f2f6b26dad8456fb881875c3c`
held after 774.06 seconds at `scope_review_preparation` with `attempt_read_limit`.
The pinned Elm commit remained `bc224a78db6f669d56863599d653f14e7605de0b`.
Twenty-three completed model calls all reported `stop`; no output was truncated.
Four independent source reviews occurred, with four coverage revisions and six
schema repairs. There were 78 proposed criteria, 32 retained findings and 23 open
findings. No scope contract, executable manifest, installer or clean consumer passed.
The required dashboard latency CI failure also remained unresolved; this experiment
was not shipping authorization.

The initial investigation read 56 distinct files. Subsequent review/revision rounds
continued with those same inputs. The only later source-tool call reread an existing
file. Review objections named additional files in prose, but author revisions could
be submitted without inspecting them. Repeated mandatory evidence fetches consumed
505 of 512 reads; the next eight-file preparation batch could not fit. The attempt
still had 2,826 seconds remaining. More time alone would not address this failure.

Private reproducible evidence: `review-resolution-premerge-haiku55-elm60m-high64k/elm/`
under `.agents/state/353-org-validation/`, including `result.json`,
`environment-attempt.json`, `chat-001.json` through `chat-023.json`, and
`investigation-audit.json`. The owned source directory, candidate image references
and registry were confirmed absent after termination; the pinned checkout was unchanged.

## Selected correction

Keep the current scope author, fresh independent reviewer, findings ledger and
executable pipeline. Add explicit inspection obligations to review objections and
complete their investigation before admitting another author revision. Reuse exact
previous author evidence rather than fetching its entire unchanged input set again.
Fresh independent reviews still receive complete independently verified evidence.
No automatic resolution, language catalog, project recipe or expanded budget is added.

Increasing quotas merely postpones the demonstrated loop. More prompt wording leaves
the same unenforced transition. Replacing the provider does not repair that transition.

## Inspection obligations and lifecycle

- A finding can carry bounded structured inspection requests: an exact repository
  file read or an existing paginated directory-list operation. Requests name paths
  and, for listing, the cursor; they do not contain shell commands or URLs. They
  are separate from `sources`, which continues to mean already inspected evidence.
- The daemon binds each admitted request to the assigned finding ID, current scope
  digest, source commit and complete inventory digest. Exact duplicates within a
  request set are rejected or reported for correction, never silently discarded.
  Proposed bounds are eight requests per finding and the existing 32-finding cap.
  Existing source admission, output, byte, read and deadline limits still apply.
- Following a `revise` verdict, pending requests form an explicit investigation
  queue. A revision cannot be submitted until every queued request has an admitted
  result available to its author. Inspection uses existing generic evidence tools;
  it neither executes repository code nor generates a manifest.
- A missing path gets pinned-inventory absence evidence. An unavailable, denied,
  oversized or interrupted read remains unavailable and holds; it is not equivalent
  to absence. Directory listings prove only their returned page, not inspection of
  listed file contents or of remaining pages.
- The author must answer the associated findings using the investigation results.
  For successful file reads, the response must cite the requested file. Results
  and finding responses are delivered to the next fresh independent reviewer.
  Inspection alone never resolves a finding, proves applicability, or grants
  coverage. Every retained finding still needs the reviewer's explicit decision.
- Reviewers can investigate directly using existing tools before issuing a verdict.
  The explicit queue covers requests they hand to the author. Source conflicts
  already established by inspected content remain legitimate blocking findings;
  an empty queue does not imply that a scope is acceptable.

## Evidence reuse, bounds and recovery

Retain bounded exact author evidence from the current attempt. A revision can reuse
that content only when its commit, inventory, path and content hash still match the
retained provenance. New evidence is inspected through normal admission. Cached
content must pass the same per-phase complete-context and source-byte checks; this
is not permission to omit mandatory input from the author conversation.

The author cache avoids redundant author fetches. It does not replace the fresh
reviewer's complete evidence fetch and hash verification. Count actual source
inspection against the existing read limits and account repeated context delivery
separately; do not reset counters or reinterpret prior recorded reads retroactively.
There is no increase to the current 512 aggregate reads, phase/context ceilings,
repair allowances, model call cap or original absolute deadline. Any exhausted
bound still holds, including when investigation cannot fit.

Version the changed review/journal representation. Resume only with matching
identity, intact evidence, pending requests and original budget. Missing cache data
requires normal bounded reinspection or a hold. Legacy held attempts stay terminal;
unsupported pending-request state cannot be treated as an empty queue or acceptance.
Never log raw source content, credentials or arbitrary rejected model values.

## Acceptance scenarios

1. A reviewer requests an unfamiliar nested file. The next revision is blocked until
   its pinned content is delivered; no filename, framework or language recognition
   is involved. A response omitting the new evidence is rejected atomically.
2. A revision tries to proceed with a denied or failed read. It holds with the
   original finding open. Pinned absence remains distinguishable from read failure.
3. The author rereads a known file while another request is pending. The pending
   request remains unsatisfied; repeated evidence cannot count as investigation.
4. Several revisions reuse the same 56 author inputs. They avoid redundant author
   fetches while each fresh reviewer receives and verifies the full input set.
   Hash drift, altered identity and oversized context are rejected.
5. A completed request leaves a genuine source conflict. The independent reviewer
   can keep it open; no automatic duplicate handling, scope reduction or acceptance
   occurs. Failed/unrun executable checks remain impossible to waive.
6. Cancellation and recovery preserve pending requests, completed result identities,
   ledger history and spent budgets. Cleanup failures hold. No deadline renews.
7. Replay the saved Elm responses without model or project execution to reproduce
   the old premature-revision transition and verify that it is rejected. Use a
   scripted unfamiliar repository to exercise the corrected investigation flow.
8. Run fresh pinned Elm with the same explicitly recorded provider/runtime profile.
   Success requires actual independent source and executable coverage acceptance,
   the same manifest digest through installation and clean consumption, every
   required successful check and lifecycle result, and confirmed cleanup. A scope
   proposal or completed inspection queue alone is not success.

## Scope and remaining gates

Do not change the constitution, required CI assertions, global timeouts, provider,
model effort, project source, runtime privileges, egress policy or other repositories.
The unrelated required dashboard latency failure remains a shipping blocker.
The full PR review, existing sample acceptance, Elm clean execution and final-head
required CI gates remain outstanding. This draft is a proposed correction, not an
implementation plan or acceptance claim.
