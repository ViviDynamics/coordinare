# Feature Specification: The advocate and the curator become performer runs

**Feature Branch**: `173-advocate-curator-performers`
**Created**: 2026-09-07
**Status**: Draft
**Input**: Coordinare should kick off the advocate and the board curator, and stop being either of them. Both become performer runs. Carry the persona, documentation and board-filter corrections along with the move.

## Context

Two jobs sit in front of the per-card lifecycle. One answers inbound GitHub issues from people outside the project. The other decides which issues are ready to become work on the board.

Today only the first exists, and coordinare performs it itself: the daemon reads open issues, classifies each with a single model call on its own backend, and posts the reply, the label and the escalation. The second does not exist at all, though the shipped persona describes it, which is how the two came to be confused.

That arrangement has three consequences this feature removes.

Coordinare acts as the role rather than orchestrating it. Every other role in the system runs in a performer, and coordinare dispatches it and reads its report. The advocate is the exception, and the exception is why it never got the bounded steps, the budgets and the schema guards that every other role now has.

The model's answer reaches the public unchecked. The classifier is told to answer only from the documentation and to name the file each claim came from. The names it returns are parsed and then ignored, and the answer is posted verbatim onto a public issue. Nothing verifies that a named document exists or that the claim appears in it. This is the last place in the system where a model writes to a human with no grounding check, and it is the opposite of the discipline every recent role follows.

The persona describes the second job while attached to the first, and it arrives inside the documentation. The classifier is handed instructions in the same value that is supposed to hold cited evidence, so evidence and orders are indistinguishable, which is also why no grounding check is possible until they are separated. The public documentation repeats the wrong description, and the lifecycle walkthrough lists the advocate as the first of nine per-card stages when it is not part of the lifecycle at all.

Neither role owns a card. There is precedent for that: the wiki seeding run is a card-less performer dispatch driven straight from the daemon, and the environment bootstrap is another. This feature follows the wiki pattern rather than inventing one.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The advocate answers from documentation it can prove it read (Priority: P1)

Someone outside the project opens an issue asking how something works. Coordinare starts an advocate run. The run reads the project's documentation, classifies the issue, and either replies with an answer whose every cited document is one it actually read, or escalates to a human. Coordinare posts nothing and classifies nothing; it starts the run and records the outcome.

**Why this priority**: This is the whole point of the change. It moves the role out of coordinare and closes the grounding hole in the same motion, because the grounding check is only possible once instruction and evidence are separate values. Delivered alone it is already a complete improvement: the role behaves better and coordinare is smaller.

**Independent Test**: Enable the advocate, open an issue answerable from the documentation and another that is not, and run one cycle. The first receives an answer citing a real document; the second is escalated, not answered. Coordinare makes no classification call.

**Acceptance Scenarios**:

1. **Given** an inbound question the documentation covers, **When** an advocate run completes, **Then** a reply is posted whose cited documents are all documents the run read, and the issue carries the handled label.
2. **Given** an inbound question the documentation does not cover, **When** an advocate run completes, **Then** no answer is posted, the issue is escalated to a human with a reason, and a notification is raised.
3. **Given** an answer naming a document the run never read, **When** the run's grounding check evaluates it, **Then** the answer is withheld and the issue is escalated rather than replied to.
4. **Given** an issue matching a sensitive keyword, **When** the run processes it, **Then** it is escalated before any model call is made.
5. **Given** the advocate is disabled, **When** a cycle runs, **Then** no run is started and no issue is touched.
6. **Given** an advocate run, **When** it finishes, **Then** it has opened no pull request and pushed no branch.

---

### User Story 2 - The curator proposes work, and a human decides (Priority: P2)

An issue in the repository describes a well-scoped piece of work. Coordinare starts a curator run. The run judges the issue against the selection criteria, and when it qualifies, adds it to the board's backlog with a label and a comment saying why. A human moves it into the working column. The curator never puts work into the pipeline itself.

**Why this priority**: This is a new capability rather than a correction, and it depends on the card-less run mechanism that User Story 1 establishes. It is second because the project functions without it today, by curating the board by hand.

**Independent Test**: Point the curator at a repository with one well-scoped issue and one vague one, run a cycle, and inspect the board. The first is in the backlog with a reason; the second is untouched; neither is in the working column.

**Acceptance Scenarios**:

1. **Given** an issue meeting the selection criteria, **When** a curator run completes, **Then** the issue is on the board in the backlog, carries the curator label, and has a comment explaining why it qualified.
2. **Given** an issue that does not meet the criteria, **When** a curator run completes, **Then** it is not added to the board and no comment is posted.
3. **Given** a qualifying issue, **When** the curator adds it, **Then** it is never placed in the column coordinare dispatches from, so a human decides whether it becomes work.
4. **Given** an issue already on the board, **When** a curator run processes the repository, **Then** it is not added again and no duplicate comment appears.
5. **Given** a judgement whose stated reason quotes the issue, **When** the run's check evaluates it, **Then** a reason quoting text that is not in the issue is rejected and the issue is left alone.
6. **Given** an issue the advocate previously answered, **When** card selection runs, **Then** the issue is eligible to become work, because only escalated issues are excluded.

---

### User Story 3 - The project describes the roles it actually has (Priority: P3)

Someone new reads the introduction, the quickstart and the lifecycle walkthrough. The advocate is described as answering inbound issues, the curator as proposing work to the board, and the numbered per-card lifecycle contains only roles that run per card.

**Why this priority**: Wrong documentation costs a reader time and trust, but it changes no behaviour, so it ranks below both working roles.

**Independent Test**: Read the three documents. Neither role is described as something it is not, and the numbered lifecycle has no gap.

**Acceptance Scenarios**:

1. **Given** the introduction's role table, **When** the advocate and curator rows are read, **Then** each describes the job its role performs.
2. **Given** the quickstart's role table, **When** the same rows are read, **Then** they agree with the introduction.
3. **Given** the lifecycle walkthrough, **When** its numbered table is read, **Then** neither role appears in it, the remaining roles are numbered consecutively from one, and a statement below says both run outside the lifecycle.

---

### Edge Cases

- What happens when a run is already in flight and the next cycle comes round? No second run is started for that repository, and the in-flight marker is set before the dispatch is attempted, so a dispatch that fails immediately cannot leave the marker stuck.
- What happens when the daemon restarts between starting a run and recording its outcome? The in-flight marker does not survive the restart, so no run is permanently blocked. Whether an issue was already handled is determined from its labels and its presence on the board, which are durable, and never from memory alone.
- What happens when the documentation cannot be read at all? The run escalates naming that as the reason rather than answering from nothing.
- What happens when a run exceeds its time budget or the container dies? The outcome is recorded as a failure with a reason, the in-flight marker clears, and the next cycle may try again under a cooldown that lengthens with repeated failures.
- What happens when the model returns an answer for an issue the run never sent it? That judgement is discarded, exactly as the reviewer and closer discard judgements about work they did not raise.
- What happens if a new role reaches the shared end of the performer's status handling? It must not. Every role added here returns its own terminal outcome, because the shared tail runs lint, pushes the branch and opens a pull request.
- What happens in a deployment with the roles unconfigured? Nothing runs, nothing is posted, and the deployment behaves exactly as it does today.

## Requirements *(mandatory)*

### Functional Requirements

#### Both roles run as performer runs

- **FR-001**: Coordinare MUST start each role as a performer run and MUST NOT itself classify issues, post comments, apply labels or add items to the board on either role's behalf.
- **FR-002**: Each run MUST proceed as an ordered sequence of bounded steps inside the performer, with a call budget and a guarded response shape, as every other role's run does.
- **FR-003**: A run MUST be startable without a card, and MUST NOT require or invent lifecycle state that belongs to a card.
- **FR-004**: At most one run per role per repository MUST be in flight at a time. The in-flight marker MUST be set before the run is started and MUST be cleared if starting it fails.
- **FR-005**: The in-flight marker MUST NOT survive a restart, so an interrupted run cannot block the role permanently.
- **FR-006**: Each role MUST return its own terminal outcome, and MUST NOT reach the shared end of the performer's status handling, which pushes a branch and opens a pull request.
- **FR-007**: A run MUST NOT open a pull request, push a branch, or commit to the repository.
- **FR-008**: A run's outcome MUST be recorded durably, and MUST be flushed rather than waiting on a lifecycle change that a card-less run never produces.
- **FR-009**: Runs MUST be rate limited so a role does not run on every poll, and repeated failures MUST lengthen the wait before the next attempt.

#### The advocate

- **FR-010**: The advocate MUST classify each unprocessed open issue, and MUST escalate an issue matching a sensitive keyword before any model call is made for it.
- **FR-011**: The advocate MUST answer only from documentation the run actually read, and every document its answer cites MUST be one the run read.
- **FR-012**: An answer that cites a document the run did not read, or that cites nothing, MUST be withheld, and the issue MUST be escalated instead of answered.
- **FR-013**: A judgement about an issue the run did not send for classification MUST be discarded.
- **FR-014**: The advocate MUST preserve today's outcomes: the distinct handling of a complaint, a feature request, a bug report and an off-topic issue, the confidence threshold applied to questions and confusion, applying the label before posting the comment, the escalation reasons and their notifications.
- **FR-015**: An escalation MUST record which reason caused it, and MUST raise the notification it raises today.
- **FR-016**: Whether an issue has already been handled MUST be determined from durable evidence on the issue itself, not from state held only in memory.

#### The curator

- **FR-017**: The curator MUST judge each candidate issue against the selection criteria with one guarded model judgement per issue.
- **FR-018**: A qualifying issue MUST be added to the board's backlog, MUST NOT be added to the column coordinare dispatches from, and MUST carry a label and a comment stating why it qualified.
- **FR-019**: A judgement whose stated reason quotes text that does not appear in that issue MUST be rejected, and the issue left untouched.
- **FR-020**: An issue already present on the board MUST NOT be added again, and MUST NOT receive a duplicate comment.
- **FR-021**: The curator MUST NOT edit issue bodies or acceptance criteria, and MUST NOT move any card between columns.

#### Selection eligibility

- **FR-022**: Card selection MUST exclude only issues escalated to a human, so an issue the advocate merely answered can still become work.

#### Personas and documentation

- **FR-023**: A configured persona MUST reach the model as instruction, and MUST NOT be placed inside the documentation the model is asked to answer from and cite.
- **FR-024**: Each role's shipped persona MUST describe the job that role performs, and MUST NOT instruct the model toward an outcome the system prevents.
- **FR-025**: The introduction, the quickstart and the lifecycle walkthrough MUST describe both roles accurately, MUST omit both from the numbered per-card lifecycle, MUST number the remaining roles consecutively from one, and MUST state that both run outside the lifecycle.

#### Retirement and regression

- **FR-026**: The in-daemon classification path this feature replaces MUST be removed rather than left dormant beside its replacement.
- **FR-027**: With neither role configured, the system MUST behave exactly as it does today, and both roles MUST remain disabled by default.
- **FR-028**: Each rule introduced by this feature MUST have a test shown to fail when that rule alone is reversed.

### Key Entities

- **Run**: one bounded execution of a role over a repository. Owns no card. Has an outcome, a duration and a record of what it did.
- **Issue candidate**: an open issue a run is considering, with its text, its labels and whether it is already on the board.
- **Documentation set**: the project files a run read, each with a name an answer may cite. Nothing a run did not read belongs to it.
- **Classification**: the model's judgement about one issue, with its confidence, its answer where it has one, and the documents it claims to cite.
- **Selection judgement**: the model's verdict on whether one issue is ready to become work, with a reason quoting that issue.
- **Escalation**: a decision to hand an issue to a human, with the reason, the label and the notification it raises.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero answers are posted whose cited documents were not read by the run that posted them.
- **SC-002**: Zero classification calls are made by coordinare itself; every one happens inside a run.
- **SC-003**: Zero pull requests and zero branch pushes originate from either role.
- **SC-004**: Zero issues are placed by the curator into the column coordinare dispatches from.
- **SC-005**: Zero duplicate replies or duplicate board additions across a restart, verified by restarting between a run and the next cycle.
- **SC-006**: An issue matching a sensitive keyword costs no model call.
- **SC-007**: Every previously passing behaviour of the classification branches, the confidence threshold, the labels, the escalation reasons and the notifications still passes.
- **SC-008**: With neither role configured, the system's behaviour is unchanged and both remain disabled by default.
- **SC-009**: A run of either role completes within 6 minutes at the 90th percentile, including container start.
- **SC-010**: A repository with nothing to do costs zero model calls, so an idle project is free to poll.

## Assumptions

- Both roles are dispatched directly by the daemon in the manner the wiki seeding run already uses, not through the per-card dispatch path, which requires a card and would refuse the run.
- A performer always receives a working copy of the repository, so documentation is read from that working copy rather than fetched file by file over the API. A deployment that points documentation at a branch other than the default one needs that branch made available to the run.
- The performer can already post issue comments. Applying a label and adding an item to a board are capabilities it does not have yet and this feature adds, along with conveying the board's identifier to the run, which nothing does today.
- The sensitive-keyword escalation stays a plain rule evaluated before any model call, keeping it deterministic and free.
- Comment attribution continues to identify the run that posted, now produced by the run rather than by coordinare.
- The environment-cache readiness gate that holds card dispatches does not apply to these runs, which need no toolchain. A run may therefore start when that cache is not current.
- Both roles remain disabled by default, and the running deployment configures neither, so this feature changes nothing observable there until someone enables one.

## Out of Scope

- Any change to the per-card lifecycle, its stages, or how cards are dispatched, beyond narrowing which issues card selection excludes.
- Aggregating a judgement across more than one model provider. The multi-provider path retires with the code that hosts it, and a run makes one judgement.
- Changing the fixed page size of the issue scan.
- Automating promotion out of the backlog. A human moves the card.
- Any web interface for reviewing what either role proposed.
