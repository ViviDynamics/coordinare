# Repository validation results

## Elm — passed, 2026-10-18

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
Provenance was frozen and readiness accepted. Every required result has exit code 0.

The optional Docker CLI installation and availability check failed. These failures
remain in the evidence. The manifest declared daemon-backed Compose, browser and
hardware lanes outside its environment scope before independent acceptance; no
Docker daemon or deployment readiness is asserted.

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

## Remaining validation

The same code and profile are being tested against `conductor-e2e-sample`, followed
by the other 27 repositories in the original 29-repository inventory. Their results
are pending. This document does not claim the repository matrix or PR is complete.
