# Repository validation results

## Latest Elm source-review run: held on read budget, 2026-10-20

Coordinare `ea4f4c33825cae8f2f6b26dad8456fb881875c3c` tested the pinned Elm
commit `bc224a78db6f669d56863599d653f14e7605de0b` in a fresh isolated
pre-merge experiment. Required CI was still failed on an unrelated dashboard
request-latency assertion; this run did not qualify the PR for shipping.

The attempt held after **774.06 seconds** at `scope_review_preparation`, with
`attempt_read_limit`; it did not time out. All 23 model responses reported `stop`.
Four independent source reviews and four coverage revisions occurred. Six schema
repairs were consumed. The final proposed definition had 78 criteria, with 32
retained findings and 23 still open. No source-scope contract was accepted.
**No executable-manifest review, installer or clean consumer ran. Readiness remains
unproven.** The harness's legacy top-level independent-review count tracks executable
review only; the four source reviews are recorded in `diagnostics.scope_review_count`.

The initial author inspected 56 distinct files. Subsequent revisions retained the
same 56 inputs despite review objections identifying additional files to inspect.
The only later source-tool operation reread an existing file. Mandatory evidence
redelivery consumed 505 of 512 reads; the next eight-file batch could not fit the
remaining seven reads. The attempt still had 2,826 seconds left. Increasing time
alone cannot resolve this observed loop. The proposed
[source-investigation correction](source-investigation-amendment.md) has written-spec
and implementation-plan approval. Its generic request queue, author-only cache
and interruption recovery are implemented and independently reviewed. Deterministic
regressions cover unknown nested paths, incomplete evidence, exact listing pages,
missing cache, immutable origin identities and original finite budgets. The 56-file
cache regression performs 168 fetches instead of 224 while retaining two complete
fresh independent reviewer fetches. This correction has not yet been run against Elm.

The offline replay processes all 23 saved responses without executing commands or
inventing new inspection requests. It counts six source-assessment response proposals
and seven revision proposals, including schema-rejected proposals; the durable
journal records four admitted source reviews and zero executable reviews. The one
post-author source-read tool call selected an existing path, with no new file selected.
All six old assessment responses lack the newly required explicit request fields
and are rejected by the current source-review protocol. That protocol rejection
is not a simulated successful investigation or an Elm acceptance claim.

Profile: `claude-haiku-5-5`, high effort, authenticated local LiteLLM connection,
strict JSON schema, 65,536 output tokens, 2 MiB conversation ceiling, 3,600-second
attempt/request/phase/installer limits, 1,800-second private probe batch and 8/4/2
schema/coverage/execution allowances. The existing immutable optional VM base and
resource/isolation policy were retained. This separately labeled experiment does
not renew an earlier held attempt or replace historical baseline results.

Private evidence is under `.agents/state/353-org-validation/` in
`review-resolution-premerge-haiku55-elm60m-high64k/elm/`: `result.json`,
`environment-attempt.json`, all 23 response files and `investigation-audit.json`.
The separate image directory contains `cleanup-verified.json`. All four owned
Docker image/registry references and the owned source directory were confirmed
absent; the pinned checkout was unchanged. Broader repository runs remain stopped.

## Earlier Elm correction test: held before installation, 2026-10-18

Code `41fc772259e925a36c6a6613ee20ca56f5cf0ff2` was tested against the same
pinned Elm source with the approved low-effort model and 3,600-second profile.
The actual attempt terminated after 650.6 seconds with `attempt_schema_limit`,
not a timeout. Eight calls completed; the original two schema repairs and one
coverage revision were consumed. Complete-declaration recovery worked, and
independent review raised four blocking findings. The next declaration mapped
a required Rails requirement to an optional runtime check, which admission
correctly rejected. No installer or clean consumer ran; readiness is false.

The review still excused client-only coverage for documented service needs.
More protocol corrections have therefore not established reliable whole-project
coverage. The proposed [coverage-first amendment](coverage-first-amendment.md)
records the architectural correction and awaits written-spec approval; it is not
implemented or evidence of readiness. Raw evidence is retained under private
`complete-correction-elm60m-low/elm/`. The source snapshot is unchanged, and
owned image references, registry container and registry volume were independently
confirmed absent in `cleanup-verified.json`. The broader matrix remains stopped.

## Elm — execution passed; coverage acceptance invalidated, 2026-10-18

A subsequent independent source audit rejected the model's coverage judgment.
The successful execution below remains valid evidence for the declared commands,
but **does not establish readiness for the approved project environment**.
Docker-backed prerequisites were excluded because the consumer lacked a daemon;
required Node dependencies and promtool were omitted, and offline Go usability was
not demonstrated across every promised module. The approved fixed scope cannot be
reduced to match the available image. SC-004 remains unmet; do not merge this PR.

Validated code: `bc3ed3cdd7c639df630f48fcc18112b05bb8f513`.
Pinned repository: `ViviDynamics/elm` at
`bc224a78db6f669d56863599d653f14e7605de0b`.

The isolated bootstrap completed in 1,011.23 seconds. Independent coverage review
accepted the manifest after one coverage revision. Five required installation
actions passed. All six required checks passed both during installation and in a
fresh consumer: a Go build of control-plane, golangci-lint against finance-platform,
pinned ShellCheck execution, the elm-host Helm chart lint, Python tooling imports,
and Node/npm availability. These are the reviewed manifest's environment checks;
this result does not claim Elm's entire application or deployment test suite passed.

The consumer ran with networking disabled, no privileges, and the cache mounted
read-only. Installer and consumer evidence share this manifest digest:
`d9a0339fd946ecbc7aabdb736720236fd3ac89bb330ae5a8459ef0f035c01d98`.
Both bind the same pinned source, immutable image and `linux/aarch64` platform.
The execution source contains 6,616 tracked files, 77,426,246 bytes, with digest
`6ba372143332703eb4275ae39e01b166c30428960d94b636c1f74652bc4a042d`.
The original run froze provenance and recorded readiness. That readiness decision
is invalidated by the coverage audit above. Every declared required result has exit
code 0; omitted source obligations still prevent acceptance.

The optional Docker CLI installation and availability check failed. These failures
remain in the evidence. The manifest excluded daemon-backed lanes because the
consumer lacked a daemon. Independent review initially accepted that exclusion
incorrectly; the later audit rejects it. No Docker daemon or deployment readiness
is asserted.

The reviewer rejected an unpinned ShellCheck package installation. Discovery
replaced it with the exact upstream release and the reviewer resolved that finding.
A missing review decision status was then corrected using a 113-byte
`repair_proposal` response. Full assessment admission ran again; no required check
or failed execution was waived. The attempt used eight model calls, one schema
repair, one coverage revision and zero execution repairs, within the original caps.

Profile: `spark/glm-5.3-flash`, low effort, 32,768 completion tokens, explicitly
selected `json_object` transport, and user-authorized 3,600-second test deadlines.
Production defaults are unchanged. Earlier failed baseline and experimental
results remain retained; this successful tuned profile does not replace them.
No language or framework recipes were added to Coordinare's core.

Private local evidence lives under
`.agents/state/353-org-validation/targeted-repair-elm60m-low/`: the accepted manifest
and assessment, installer output, clean evidence in `elm/result.json`, exact scope
inspection, and `cleanup-verified.json`. All owned containers, images, registry
volumes and source staging were independently confirmed absent after completion;
the pinned snapshot is unchanged. The live daemon and outer retry loop were not
exercised by this harness.

## Small sample — passed, 2026-10-18

The same product code and profile validated `ViviDynamics/conductor-e2e-sample` at
`64dbb0b613884a024691a8e3eb1724ea4a1fc787` in 263.24 seconds. Independent review
accepted the manifest. Its six required checks passed in both installer and clean
consumer: Python version assertion, pytest and Ruff availability, package import,
the actual Ruff lint command, and the actual pytest suite. No installation was
necessary because the base image supplied the tools. Provenance was frozen with
manifest digest `da3592892c525f723611907dfe897ee48aa2621c0b52a5f0df1fd01d5269ace6`.
The attempt used one schema repair and no coverage or execution revisions.

Owned resource absence and the unchanged snapshot were verified separately.
Private evidence is under
`.agents/state/353-org-validation/targeted-repair-sample60m-low/`. This establishes
the small-sample part of SC-004 for the explicitly labeled profile. Elm still lacks
complete coverage; neither the earlier baseline nor universal readiness is claimed.

## Remaining validation

The broader run was stopped when the Elm coverage audit invalidated its
prerequisite. Twenty-one repositories produced terminal results and six were not
started. Four terminal results recorded successful declared checks; they do not
substitute for the missing Elm acceptance or establish universal coverage. All 27
pinned source snapshots remain unchanged. Owned image, registry, volume and source
staging absence was independently verified after cancellation. Partial results
remain under the two `targeted-repair-matrix30m-low-*` profiles; cleanup proof is
`targeted-repair-matrix30m-shared-runtime/cleanup-verified.json`.

A Nare repair exposed a separate no-waiver defect: after `pytest -q` failed, the
model retained its required ID but added exclusions and a retry wrapper. Execution
approval was withheld. The correction preserves every required check and service
command definition after any project command executes, including unrun checks and
all earlier rejected generations. Only check timeout increases are allowed;
installation and activation remain repairable. Trusted preflight that stops every
command before execution can still correct a declaration. Pins are derived from
existing bounded rejected evidence and survive daemon snapshots and cached
acceptance. Fresh reviewers receive the prior manifests and per-command outcomes,
without output bodies, to assess whether installation or activation changes weaken
the source obligation. Exact command identity alone cannot prove semantic coverage.

The real Nare rewrite is rejected with `obligation_removed`; its original command
remains admissible against the same execution evidence. Relevant local tests pass:
716 passed, 14 existing platform skips. Strict type checking covers 339 modules;
lint and the existing debt ratchet pass without new suppressions or baseline edits.
The full regression suite will run in CI for the correction.

## Actual runtime limitation

Dependency caching, tool installation and ordinary subprocess services fit the
existing generic manifest. Full Elm coverage additionally requires a usable
container service. The existing Docker clean verifier has no daemon socket and
uses `--network none`; Kubernetes drops capabilities and prohibits privilege
escalation. A probe of the actual immutable base image under the Docker verifier's
network isolation confirmed `unshare -Ur true` fails with `Operation not permitted`.
A rootless nested-runtime recipe therefore has no demonstrated supported path on
this target. Installing another CLI or extending the timeout cannot supply that
kernel capability. No host socket, privileges, verification networking or narrowed
project scope have been introduced. An isolated runtime solution must be established
before another Elm readiness claim or a broader repository run.

A subsequent [private-runtime experiment](runtime-feasibility.md) proved that a
software-emulated guest can run a private Docker container and Compose workload
inside the existing isolation boundary. Its fresh offline consumer passed in
95.11 seconds with the guest base unchanged. This is feasibility evidence, not
Elm acceptance. A separately labeled `vm-tools-elm60m-high` run tested
normal agent-generated manifests with optional platform tools and high reasoning
effort. No prepared guest or edited manifest was supplied; all gates remained active.
That run held in review before installation after 1,261.65 seconds. A capability
flag contradiction was misreported as a citation error, so citation corrections
could not resolve it. The correction returns the actual trusted validation reason
and field, preserving objections and the original correction allowance. A regression
reproduces that failure and requires a subsequent independent resolution of the
retained coverage finding. Source navigation guidance also explicitly requires
following referenced development instructions and distinguishes missing tools from
unproven or impossible capabilities. A read-only replay tests the model's response
before another full Elm execution.

The subsequent `accurate-review-elm60m-high` profile held in review after 1,326.00
seconds, with no installation or clean execution. It read the referenced test
instructions and the reviewer retained four blocking findings: console coverage,
other omitted development lanes, a pinned linter version, and container-service
capability. One schema correction removed unsupported envelope fields. No coverage
revision was attempted: the capability flag incorrectly took precedence over the
reviewer's explicit `revise` verdict. The snapshot remained unchanged; owned image,
registry and volume absence were verified. Raw results are retained separately.

The control-flow correction gives `revise` the existing bounded coverage-repair
path, including capability objections. This implements the approved persistent
findings amendment's revision semantics. It does not accept the manifest or close
the capability finding. An indeterminate capability judgment still holds
immediately, trusted runtime-configuration failures remain terminal, and exhausted
coverage allowance still holds. A new independent reviewer must explicitly
adjudicate every retained finding before any installation is admitted.

Four failing-then-passing regressions cover independent capability resolution and
unchanged coverage limits of zero, one and two. Replaying the exact live assessment
with its 15 independently verified source hashes now reserves one coverage
revision and preserves all four open findings. This transition replay executes no
model or project commands and is not bootstrap acceptance evidence. Independent
review found no important issues and passed 78 focused regressions.

Preflight guidance now asks for bounded discovery of available command names as
well as expected prerequisites, so unfamiliar execution mechanisms can be found
without a built-in platform or technology catalog. An inventory remains a hint,
not service-usability evidence. The existing two-batch, command, output and time
bounds are unchanged. A read-only inventory of the retained diagnostic image
returned 817 names in 7,842 bytes, including its software-guest executable; this
is not a probe result or readiness proof for a new Elm attempt.

The subsequent full-diff adversarial review found a Kubernetes restart-cleanup
bound mismatch: source transport admits up to 257 Secrets, but the orphan sweep
skipped Pods above 64. A failing regression used a validated 32 MiB binary source
bundle that the real builder split into 66 Secrets. The sweep limit now derives
from the shared transport limits, including metadata and legacy evidence. The
ownership checks still run before Pod or source deletion. Tests also exercise 257
owned Secrets, a foreign UID at the final Secret, and an over-bound annotation that
must perform no Secret reads. All 44 focused source/lifecycle tests passed; the
reviewer independently verified the correction. These are unit/API-double results,
not a live Kubernetes bootstrap claim. The active Docker Elm run continues unchanged.


## Candidate disclaimers cannot redefine requested coverage

The `repairable-capability-elm60m-high` profile at `14c58d09` reached pipeline
acceptance after 1,581.11 seconds, nine completed calls, two schema corrections
and one coverage revision. Its exact manifest digest was
`40e7d1edf4a71f3b8a1db262b4aeda8bcace1b1cc51a78169f9f0711269d9d0a`.
A separate source-first independent agent audit rejected that final artifact.
Only three Go test checks had been added; installation and activation were
unchanged and services remained empty. The candidate explicitly disclaimed
service-backed readiness, and the pipeline reviewer treated that disclaimer as
permission to omit part of the requested environment. It was not permission.
The full environment still lacked required service operation and other documented
dependency, version and usability coverage. Raw acceptance is retained alongside
explicit coverage invalidation; no execution approval was issued.

The correction makes scope ownership explicit in trusted discovery/review context:
the whole documented development/test environment has no operator-authorized
exclusions. Candidate descriptions and disclaimers cannot change that scope.
Missing required capabilities remain blocking findings, and resolving known
findings does not replace a fresh omissions review. Optional and deployment-only
activities still require repository-based applicability judgments. This is the
approved persistent-findings amendment's existing scope rule, not a new ecosystem
catalog, execution permission or automatic scope selector.

A fresh read-only replay against the exact rejected candidate, same pinned sources
and same GLM model now rejects the CLI-only disclaimer and identifies additional
omitted project areas. After two existing schema corrections, that replay produced a valid `revise`
assessment with four blocking findings. It is not bootstrap success.
The replay and all historical artifacts remain separate from executable validation.
No project commands or approvals are produced by this diagnostic.

All 57 focused discovery/findings/preflight regressions passed. An independent
adversarial review of the correction passed 93 focused tests and found no critical
or important issues. Elm acceptance and the remaining repository runs are still
outstanding; broader repository execution remains paused.


## Actionable feedback for bounded proposal corrections

The live `trusted-scope-elm60m-high` comparison produced a broader candidate
with 19 requirements, 13 installation actions and 22 checks, but no services.
It explicitly retained a required service-runtime gap. The candidate first failed
structural admission because clean checks depended on install-only commands;
coverage review had not started. Its next response attempted 22 field corrections,
exceeding the existing limit of 16 correction records. The guard rejected that
response correctly, but returned unrelated duplicate-JSON/assessment guidance.
Both schema correction slots were consumed before the next response.

Invalid correction feedback now reports the trusted record and path-depth bounds,
and identifies an overfull `changes` array. Instructions explain how one complete
replacement of a common existing parent array can correct more fields while
preserving every unrelated record and obligation. The limit remains 16 records
and depth eight; replacements still undergo atomic full admission, required-ID
and execution-obligation preservation, and independent coverage review. There is
no partial-patch admission or additional repair allowance.

A failing-then-passing regression reproduces 22 required check definitions with
install-only dependencies, an overfull correction, and a valid common-array
replacement through fresh review. It verifies all required checks and command
definitions survive and exactly both original schema corrections are consumed.
All 59 focused patch/discovery/preflight tests passed; strict typechecking passed
for 339 source files. Independent adversarial review passed 95 focused cases and
found no critical or important issues. These are protocol tests, not an Elm
installation or clean-consumer result.

The high-effort run ended after 2,788.21 seconds with `attempt_schema_limit`,
811.87 seconds remaining, seven completed calls, two schema corrections and no
coverage or execution revisions. Its final response corrected only 16 of the 22
checks, leaving six invalid dependencies. Coverage review, installation and clean
verification never ran. An upstream HTTP 500 was retried by the existing bounded
client before that final response. The source snapshot remained unchanged; owned
image, registry and volume absence were verified. No restart or new feedback was
injected into that attempt.

The exact low-effort, read-only correction replay independently verified 24 source
hashes but held on malformed nested replacement JSON. Allowing a complete
corrected declaration was already supported by the local phase parser; however,
the instructions prohibited it and structured repair schemas exposed only patch
and source actions. Independent review reproduced that schema mismatch before
push. A second permissive-fallback replay still produced a malformed patch.
Neither diagnostic executed project commands, issued approvals or established
coverage; their failed raw replies remain separate from executable validation.

For an overfull patch, trusted feedback now asks explicitly for the complete
corrected declaration rather than another partial patch. General repair guidance
permits the same existing declaration format for large nested replacements, and
manifest instructions include the required `finding_responses` field. Both native
function and strict-wire repair schemas now retain the current phase's complete
declaration alongside bounded patches. No new response type, provider downgrade,
execution permission or repair allowance is introduced.

The transport regression failed for both manifest and assessment repair schemas
before the fix. It now verifies full-declaration schema admission, native function
publication and roundtrip, strict-wire roundtrip, mixed-response rejection and
continued rejection of 17-record patches. Both common-array and full-declaration
recovery preserve all 22 required synthetic checks and consume exactly the two
original schema corrections. All 123 focused protocol/obligation tests passed,
and independent adversarial review confirmed the transport finding resolved with
no further important findings.

The final bounded, read-only low-effort replay passed structural admission after
the explicit complete-declaration request. It independently verified 24 source
hashes, preserved all 22 checks (21 required and one already optional), retained
every command field except the invalid install-only dependencies, and consumed
exactly both original schema corrections. Only one new live correction response
was requested after replaying the two saved replies. No coverage review, project
command or approval ran. The candidate's service-runtime omission remains;
structural correction does not establish environment readiness.

A fresh full Elm run will test the corrected pipeline with the same model, the
approved low effort and 60-minute budget, optional platform tools and unchanged
repair caps. This varies both the corrected implementation and effort relative
to the retained high-effort result, so it cannot isolate an effort-only effect.
Broader repository runs remain paused and Elm readiness is unproven.
