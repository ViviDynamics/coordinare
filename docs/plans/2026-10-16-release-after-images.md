# Release goes live before its performer images exist (install hits manifest unknown)

Issue #332

## Scope

Create the git tag after validation, build images from that tag, and create the
draft GitHub release only after both performer manifests and the daemon image
succeed. Preserve package upload validation, draft promotion, and rerun handling.
Image build fixes tracked by other issues are outside this change.

## Assumptions

- The draft release staging added since the issue was filed remains necessary for
  uploading and verifying package assets before publication.
- Image failures must prevent both release creation and publication through normal
  successful-job dependencies, without unconditional execution.

## Tasks

- [x] Add failing workflow graph and tag-only regression tests using the existing
  YAML and Node release test patterns.
- [x] Split tag creation from image-gated draft creation and wire package jobs to
  the draft metadata, preserving rerun idempotence.
- [ ] Run release and installer regressions, the full suite, lint, and preflight.
  Track Copilot review and merging through ship-issue checkpoints.
