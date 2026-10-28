# Deployment-image obligations amendment

Status: decided by the maintainer on 2026-10-27, after diagnostic d3.

## Problem

Diagnostics d1, d2 and d3 installed the same reviewed candidate in the private runtime guest without model calls. d3
was the first with complete per-command evidence. Every run stalled at the same step: building the repository's own
production and QA application images inside the guest, which the runtime emulates. One such step ran past its
3,600-second command limit while still installing gems inside an image build. These images exist only for the
scope's compose-stack criteria; the development and test environment itself needs a language runtime, dependencies,
a database and services, and the ability to run the tests and linters.

## Decision

Building or running the repository's own application container images or deployment stacks (production or QA images
built from its Dockerfiles, or compose stacks that run the application itself) is a deployment concern outside the
development and test environment an agent needs. Such an obligation is classified `not_applicable`, with a rationale
naming the image or stack in the cited source.

Installing the container tools, pulling or starting third-party service images that development and tests use, and
everything the application needs to run its own tests outside a container stay required.

## Mechanism

It reuses the remote-state and runtime-network mechanism:

- The shared authoring and review guidance carries a deployment-image rule beside the other two.
- Reviewers check every required criterion against all three rules. A failing criterion is a finding with category
  and gap kind `deployment_image`, naming the criteria.
- `released_criteria` and executable-phase `release_requests` treat `deployment_image` like the other release
  categories, so the author may reassess those criteria as `not_applicable` and obligation preservation honours it.

A reused scope always gets one fresh review, so the rule applies from the next run without new scope authoring.
