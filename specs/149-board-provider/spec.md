# Feature Specification: BoardProvider Protocol

**Feature Branch**: `149-board-provider-protocol` | **Created**: 2026-08-30
**Issue**: [#203](https://github.com/ViviDynamics/coordinare/issues/203)

## Overview

Coordinare reads work from GitHub Projects and nowhere else. Teams whose work lives in Jira
cannot run it at all, even though coordinare's actual job — dispatching performers, opening PRs,
driving reviews — has nothing to do with where the board is.

This spec covers **only the seam**: a `BoardProvider` protocol, with the existing GitHub behaviour
moved behind it and nothing else changed. Jira is the reason the seam exists; it is not in this
spec. The issue asks for this split explicitly, and it is the right one — an extraction whose
success condition is *the existing suite passes unmodified* is verifiable in a way that a
simultaneous extraction-plus-new-provider never is.

## What the survey found

Measured against `main`, rather than assumed:

- `GitHubService` has 48 methods, 36 public, and **26 are called from outside it**.
- Of those 26, roughly **nine are board operations**; the rest are code-host: pull requests,
  reviews, mergeability, diffs, file contents, branches, job logs.

So the protocol is small. That is the finding that matters, because a protocol built from what
`GitHubService` *offers* rather than what coordinare *calls* would have been three times the size
and would have dragged the code host into it.

## Clarifications

### Session 2026-08-30

- Q: Abstract the code host too? → A: No. A Jira board pairs with a git host; Jira can replace the
  board and never the PRs. Abstracting both at once would produce a seam shaped by neither.
- Q: Ship Jira in this spec? → A: No. The extraction's success condition is "no behaviour
  changed", which a new provider landing at the same time would destroy — any failure becomes
  ambiguous between the refactor and the new code.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Nothing changes for anyone (Priority: P1) — the whole spec

An operator running coordinare against GitHub Projects upgrades, and observes no difference of any
kind.

**Why this priority**: It is the entire deliverable. A seam that alters behaviour has failed at
the only thing it was for.

**Independent Test**: The existing test suite passes **unmodified**. Not adjusted, not
re-baselined.

**Acceptance Scenarios**:

1. **Given** an unchanged configuration, **When** coordinare runs a full cycle, **Then** every
   board interaction is identical to before.
2. **Given** the existing test suite, **When** it runs against the refactor, **Then** it passes
   with no edits to any test.
3. **Given** a board operation fails, **When** the error surfaces, **Then** it is the same error
   type and message as before.

### User Story 2 - A second provider is possible without touching coordinare (Priority: P1)

Someone can write a Jira or Trello provider by implementing one protocol, without editing the
daemon, the graph, or any node.

**Why this priority**: Equal-first, and the only way to know the seam is in the right place. A
protocol that still requires changes elsewhere has not separated anything.

**Independent Test**: A stub provider implementing the protocol drives a full cycle with no
coordinare source change.

**Acceptance Scenarios**:

1. **Given** a provider implementing only the protocol, **When** coordinare runs, **Then** it works
   without any conditional on provider type anywhere in the graph or daemon.
2. **Given** the protocol, **When** it is compared against Trello's model — lists *are* lanes,
   moves always succeed, card metadata is thin — **Then** it fits without special-casing.
3. **Given** the protocol, **When** it is compared against Jira's — restricted transitions, so a
   move can legitimately fail — **Then** that failure is expressible as an ordinary outcome rather
   than a crash.

### Edge Cases

- **A move that legitimately fails.** On GitHub Projects a move always succeeds; on Jira a
  workflow can forbid the transition. The protocol must let a provider say "no, and here is why"
  without coordinare treating it as a fault. Designing for this now costs nothing; retrofitting it
  means changing every call site.
- **Cards whose identity is not an issue number.** GitHub Projects cards *are* issues in the PR's
  repository. Jira keys are not, so nothing in the protocol may assume the card id indexes the
  code host.
- **A provider with no labels.** Trello has none in the GitHub sense. Label operations must be
  expressible as unsupported without every caller branching on it.
- **Comments in a foreign format.** Jira uses ADF, not Markdown. The protocol exchanges plain
  text; conversion is the provider's problem.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A `BoardProvider` protocol MUST exist, covering only the board operations coordinare
  actually calls.
- **FR-002**: The protocol MUST be derived from coordinare's call sites, not from `GitHubService`'s
  surface.
- **FR-003**: The existing GitHub behaviour MUST be available through the protocol with **no
  behaviour change**, evidenced by the existing suite passing unmodified.
- **FR-004**: No caller — daemon, graph node, or service — may branch on which provider is in use.
- **FR-005**: The protocol MUST express a move that legitimately fails as an outcome, not an
  exception representing a fault.
- **FR-006**: The protocol MUST NOT assume a card id addresses anything on the code host.
- **FR-007**: Operations a provider cannot support MUST be declarable, without callers testing for
  provider type.
- **FR-008**: Code-host operations MUST remain outside the protocol and MUST NOT be abstracted.
- **FR-009**: Card status MUST be exchanged as coordinare's canonical `CardStatus`, never a
  provider's native lane name.

### Key Entities

- **BoardProvider**: the protocol — poll, move, read a card, post a card comment. Reading a
  card's comments was deliberately absent here: coordinare addressed comment reads by GitHub
  issue number while every other operation used a node id, so the method could not honour a
  `card_id` contract without an extra lookup per cycle. **Closed by spec 153 (#232)**, which
  gave cards one id model and put the translation inside the GitHub adapter, where the pairing
  `poll_board` already fetches makes it free.
- **GitHub Projects provider**: today's behaviour, relocated.
- **Move outcome**: succeeded, or refused with a reason. Consumed through `move_card_or_warn`,
  which every call site uses instead of `move_card` directly — a returned outcome that nobody
  reads would leave a refusal as silent as no outcome at all.

## Success Criteria *(mandatory)*

- **SC-001**: The full existing suite passes with **no test modified**.
- **SC-002**: The protocol has no more methods than coordinare demonstrably calls.
- **SC-003**: A stub provider drives a full cycle with no coordinare source change.
- **SC-004**: No `isinstance` check or provider-type conditional exists in the daemon or graph.
- **SC-005**: No code-host operation appears in the protocol.
- **SC-006**: The protocol accommodates Trello's and Jira's models on paper without special cases.

## Assumptions

- Polling stays. Webhooks are an optimisation and would change the daemon loop, which is not what
  this spec is for.
- One board per symphony. Multiple boards feeding one symphony is a different feature.

## Out of Scope

- The Jira provider itself, and every other provider.
- Abstracting the code host.
- Webhooks.
- Card-to-PR linkage for non-GitHub boards — it is meaningless until a non-GitHub provider exists.
