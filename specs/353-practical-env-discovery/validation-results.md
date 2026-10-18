# Repository validation results

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

Profile: `onprem/glm-5.3-flash`, low effort, 32,768 completion tokens, explicitly
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
