# Runtime-network obligations amendment

Status: decided by the maintainer on 2026-10-24, after Dettmore v38.

## Problem

Dettmore's Docker Compose stacks fetch from the network every time they start:

- The dev stack's web command runs `apt-get update && apt-get install` on every container start.
- The production and QA compose files build their images from source when started.

Clean verification runs offline, so these stacks cannot start unmodified. Authors built offline workarounds
(pre-built images, compose overrides, a stub `apt-get` inside a re-tagged base image), and each executable review
found another residual network fetch or a port conflict. v38 spent six coverage revisions almost entirely on this.
v27's reviewer had raised the same objection.

## Decision

Like remote state, an obligation whose own documented procedure fetches from the network every time it runs is
outside what an offline clean verification can prove. Such an obligation is classified `not_applicable`, with a
rationale that names the network step in the cited source.

Installing the tools it uses (Docker, Compose) stays required, and so does everything installation can build, fetch
or cache in advance.

## Mechanism

It reuses the remote-state mechanism:

- The shared authoring and review guidance carries a runtime-network rule beside the remote-state rule.
- Scope reviewers check every required criterion against both rules before a verdict. A failing criterion is a
  finding with category and gap kind `runtime_network`, naming the criteria.
- `released_criteria` treats `runtime_network` findings like `remote_state` findings: the author may reassess
  those required criteria as `not_applicable`, and obligation preservation honours the release.

A reused scope always gets one fresh review, so the rule applies from the next run without new scope authoring.
