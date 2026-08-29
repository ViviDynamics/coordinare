# Feature Specification: Forward External Issue Submissions

**Feature Branch**: `152-forward-external-issues` | **Created**: 2026-08-29
**Issue**: [#210](https://github.com/ViviDynamics/coordinare/issues/210) | **Depends on**: spec 142 (#196, merged)

## Overview

Public issue submissions are the only contribution channel coordinare accepts — spec 142 closed
public pull requests by design. That promise needs a delivery mechanism: when a stranger files a
bug report, someone has to see it without watching the repository by hand.

## Clarifications

### Session 2026-08-29

- Q: Which transport? → A: **Both.** Forward to every destination that is configured and skip any
  whose secret is absent, so Slack can ship now and email can be added later without touching the
  workflow.
- Q: The issue says "fail loudly if the secret is absent", which conflicts with skipping absent
  ones. → A: Fail when **no destination at all** is configured. That is the case the criterion
  guards — a workflow that runs green while forwarding nowhere. An individually-absent destination
  is a choice, not a fault.
- Q: Summarise the issue for the reader? → A: No, and this is a security decision rather than a
  scoping one. The issue body is attacker-authored text; an AI summary of it delivered to a human's
  inbox is a prompt-injection surface for very little gain. A forwarder that only copies fields
  cannot mislead the reader about what the issue says.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A stranger's report reaches someone (Priority: P1) — MVP

An outsider files a bug report. Within one workflow run it appears in the configured destinations,
containing only what they actually wrote.

**Independent Test**: Run the extraction against a synthetic non-member issue payload; assert every
forwarded field is byte-identical to the source.

**Acceptance Scenarios**:

1. **Given** an issue from a non-member, **When** it is opened, **Then** each configured
   destination receives the title, author, template, link, and the opening of the body.
2. **Given** the forwarded content, **When** it is compared with the issue, **Then** every part is
   copied verbatim — no generated prose anywhere.
3. **Given** a very long body, **When** it is forwarded, **Then** it is truncated at a fixed length
   and the truncation is visible, so nobody mistakes the extract for the whole.
4. **Given** a body containing text that imitates instructions or system output, **When** it is
   forwarded, **Then** it is presented as quoted data and cannot be confused with the workflow's
   own words.

### User Story 2 - Maintainers do not notify themselves (Priority: P1)

An organisation member files an issue and nobody is paged.

**Why P1**: Equal-first. A notifier that fires on every internal issue is one people mute, and a
muted notifier is the same as no notifier — which is the state this feature exists to end.

**Acceptance Scenarios**:

1. **Given** an issue from an owner, member or collaborator, **When** it is opened, **Then** nothing
   is sent.
2. **Given** a maintainer whose organisation membership is private, **When** they open an issue,
   **Then** nothing is sent. The cheap author-relationship field under-reports exactly here, and it
   already caused spec 142's bot to scold a maintainer's own pull request.

### User Story 3 - A misconfigured forwarder is obvious (Priority: P2)

An operator can tell whether forwarding works without filing a test issue.

**Acceptance Scenarios**:

1. **Given** no destination configured, **When** an external issue arrives, **Then** the run fails
   visibly rather than passing having sent nothing.
2. **Given** one of two destinations configured, **When** an external issue arrives, **Then** the
   configured one receives it and the absent one is reported as skipped, not as broken.
3. **Given** a destination that rejects the request, **When** it fails, **Then** the run fails and
   names which destination.

### Edge Cases

- **A body that is empty, or entirely non-Latin, or full of control characters.** Forwarded as-is
  within the length limit; the forwarder does not interpret it.
- **A title containing the destination's own markup.** Must not be able to alter the message's
  structure — the reader has to be able to tell the issue's words from the workflow's.
- **An issue opened by a bot.**  Treated as external unless it is a known internal actor; a
  notification about a bot is cheap, a missed human report is not.
- **The secret exists but is empty.** Same as absent, and reported the same way; a whitespace-only
  secret is a configuration mistake rather than a destination.
- **The workflow runs on a shared runner.** It must never check out or execute anything from the
  submission.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: An issue opened by someone outside the organisation MUST be forwarded to every
  configured destination.
- **FR-002**: An issue opened by an owner, member or collaborator MUST NOT be forwarded, and
  membership MUST be established by a check that does not under-report private membership.
- **FR-003**: Forwarded content MUST be derived only by copying issue fields. No generated or
  summarised prose.
- **FR-004**: The body MUST be truncated at a fixed limit, with the truncation visible.
- **FR-005**: Submission text MUST be presented so it cannot be mistaken for the workflow's own
  words or alter the message structure.
- **FR-006**: Destinations MUST be configured only through repository secrets and MUST appear in no
  tracked file.
- **FR-007**: A destination whose secret is absent or empty MUST be skipped and reported.
- **FR-008**: A run with **no** destination configured MUST fail visibly.
- **FR-009**: A destination that rejects delivery MUST fail the run and name that destination.
- **FR-010**: The workflow MUST NOT check out the repository or execute anything from the
  submission.
- **FR-011**: The workflow MUST NOT change the triggers, required status, or runtime of any
  existing workflow.

### Key Entities

- **Submission**: the opened issue — title, author, template, URL, body.
- **Extract**: the verbatim, length-bounded projection of it that gets sent.
- **Destination**: a configured sink, each independently present or absent.

## Success Criteria *(mandatory)*

- **SC-001**: An external submission reaches every configured destination in one workflow run.
- **SC-002**: An internal submission produces nothing, including from a private member.
- **SC-003**: Every forwarded field is byte-identical to its source, verified by test.
- **SC-004**: No destination value appears in any tracked file.
- **SC-005**: No destination configured fails the run; one of two configured does not.
- **SC-006**: No existing workflow changes its triggers, required status, or runtime.

## Assumptions

- The advocate role remains the triage brain. This is a notification, and growing it into a second
  triage path would duplicate judgment that already exists elsewhere.
- Volume is low enough that no batching or rate limiting is needed. If that stops being true the
  fix is a digest, not a filter.

## Out of Scope

- Triage, labelling, or any decision about the issue.
- Replies to the submitter.
- Any summarisation, AI or otherwise (FR-003 is the reason).
- Forwarding comments, edits, or reopenings — only `opened`.
