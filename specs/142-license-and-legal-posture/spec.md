# Feature Specification: License and Legal Posture for Public Release

**Feature Branch**: `142-license-and-legal-posture`
**Created**: 2026-08-27
**Status**: Draft
**Issue**: [#196](https://github.com/ViviDynamics/coordinare/issues/196) (launch-blocking)
**Input**: Establish coordinare's legal and community posture for public release under the Elastic License 2.0 (ELv2): add the license, state the closed-contribution policy, and ship the baseline community and security documents every public repository needs.

## Clarifications

### Session 2026-08-27

- Q: When an outsider opens a pull request, does the automation close it or only comment? → A: Comment, apply an `external-contribution` label, then close. The label keeps the refused set queryable, since a bug an outsider tried to fix is still a bug worth triaging.
- Q: What fallback security contact does `SECURITY.md` publish? → A: None. GitHub private vulnerability reporting is the only route, so there is no address that can bounce or attract scanner traffic. This makes enabling that repository setting load-bearing (FR-016).
- Q: Should the auto-close carve out security-related submissions? → A: Yes. Security-related external submissions are commented on and labelled but left open for human review, and the detection fails towards human review (FR-025).
- Q: Does the README's prohibition need to say more than "no reselling"? → A: Yes. It must say the prohibition covers providing access to coordinare's functionality to third parties, and that it applies whether or not money changes hands (FR-005a). It must not try to adjudicate boundary cases (FR-005b).
- Q: Should the prohibition ship with a commercial route beside it? → A: Yes. A commercial-licensing pointer sits alongside it, so a firm that wants to offer coordinare to its own customers finds a door rather than a wall (FR-024). Not via a public issue template, and not via a published personal address.
- Q: What accepted license set does the FR-020 dependency check enforce? → A: Permissive allowlist (MIT, BSD-2/3, Apache-2.0, ISC, PSF-2.0, Unlicense, Zlib, and dual-licenses containing one), with MPL-2.0 and LGPL permitted as named individually justified exceptions, and GPL/AGPL/SSPL/unknown rejected. This clears the tree as it stands (`psycopg` LGPL-3.0-only, `certifi`/`orjson`/`pathspec` MPL-2.0, `semgrep` LGPL-2.1-or-later) while still failing on a future GPL addition.
- Q: Does forwarding external issue submissions to a private destination belong here? → A: No. Filed as spec 152 / issue #210 with the design recorded, so this launch-gating feature stays small. Issue templates here are structured so the forwarder is easy to add.
- Q: Is the license decision still ELv2? → A: Confirmed 2026-08-27. ELv2 carries no change date and no conversion clause, unlike BSL and FSL, so the restriction never lapses on its own. Combined with sole copyright ownership under the closed-contribution model, relicensing remains a unilateral decision.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A stranger can tell what they are allowed to do (Priority: P1)

Someone who has just found coordinare wants to know, before investing any time, whether they
may run it at their company, modify it, and self-host it, and whether they are allowed to
resell it or offer it as a hosted service. They should be able to answer all of that from
the repository root in under two minutes, without a lawyer and without reading the codebase.

**Why this priority**: This is the legal minimum to publish anything at all. Without a
license file, everyone who clones the repository holds no rights whatsoever, and the README
currently promises a `LICENSE` file that does not exist. Nothing else in this feature matters
if this is missing, and this story alone makes the repository safe to make public.

**Independent Test**: On a clean clone, a reader opens the repository root, finds `LICENSE`
and `NOTICE`, and reads the README's licensing paragraph. They can correctly state the four
permissions (run, copy, modify, self-host including for commercial work) and the two
prohibitions (providing coordinare to third parties as a hosted or managed service, reselling
it) without consulting any other source.

**Acceptance Scenarios**:

1. **Given** a clean clone of the repository, **When** a reader looks in the repository root,
   **Then** `LICENSE` contains the Elastic License 2.0 text unmodified from Elastic's published
   version, and `NOTICE` names Vivi Dynamics LLC as copyright holder and coordinare as the work,
   since ELv2's own body carries no designation fields to fill.
2. **Given** the README, **When** a reader reaches the licensing section, **Then** the
   permissions and the prohibitions are each stated in one plain-English paragraph, the
   licensing is described as "source-available", and the reader is told that future versions
   may be offered under different terms.
3. **Given** any public-facing document in the repository, **When** it describes coordinare's
   own licensing, **Then** it never claims coordinare is open source or OSI-approved.
4. **Given** the packaging metadata, **When** a distribution is built from the repository,
   **Then** the license is declared in that metadata rather than left unset.
5. **Given** the first-party package vendored in-tree at `packages/service_inference`,
   **When** a reader asks what licenses it, **Then** it is explicitly covered by the root
   license and the copyright statement, with no separate or conflicting license.

---

### User Story 2 - A would-be contributor learns the policy before wasting effort (Priority: P2)

An enthusiastic outsider wants to fix a bug they found. They should discover, as early as
possible and in friendly terms, that coordinare does not accept public pull requests as a
deliberate supply-chain security measure, and that their bug report or feature request is
genuinely wanted instead. If they open a pull request anyway, they should get a prompt,
polite, non-scolding response that points them at the issue tracker.

**Why this priority**: Second only to the license itself. A public repository with no stated
contribution policy generates pull requests that must be refused by hand, which reads as
hostile and wastes goodwill on both sides. Getting this wrong damages the project's
reputation at exactly the moment it gains an audience.

**Independent Test**: A reader following GitHub's own affordances (the contributing link
GitHub surfaces when opening a pull request, the pull request template, the issue template
chooser) encounters the closed-pull-request policy at every one of those touchpoints, and an
external pull request receives an automated policy response without any maintainer action.

**Acceptance Scenarios**:

1. **Given** `CONTRIBUTING.md`, **When** a reader opens it, **Then** it states that issues,
   bug reports, feature requests and feedback are welcome and read and that they inform the
   roadmap without any promise of triage or response, describes what a good
   report contains, and states that public pull requests are not accepted as a supply-chain
   security measure and will be closed unmerged, in friendly and unapologetic wording.
2. **Given** the issue template chooser, **When** a member of the public opens a new issue,
   **Then** distinct templates exist for a bug report, a feature request, and general
   feedback.
3. **Given** a pull request opened from a fork by someone outside the organization,
   **When** it is created, **Then** an automated response posts the contribution policy
   linking `CONTRIBUTING.md`, labels the pull request as an external contribution, and closes
   it, with no maintainer involvement.
4. **Given** a pull request opened by a maintainer or organization member from a branch in
   this repository, **When** it is created, **Then** no policy response is posted and no
   existing continuous-integration behavior changes.
5. **Given** the pull request template, **When** any pull request is opened, **Then** the
   template states the policy so an external author sees it even before the automation runs.

---

### User Story 3 - A security researcher can report privately (Priority: P2)

Someone who finds a vulnerability in an autonomous agent system that holds repository write
credentials needs a private channel that is real and clearly documented. They should not be
forced to disclose in a public issue because no other route exists. What they do not need, and
what coordinare does not offer, is a promised response time: the software is provided free of
charge with no support contract, so no timing expectation is created.

**Why this priority**: Equal in importance to the contribution policy and equally cheap. A
public repository without a reporting channel converts responsible researchers into public
disclosures. This is also the file that spec 144 will later extend, so its shape needs to be
right the first time.

**Independent Test**: A reader finds `SECURITY.md` from the repository root and from GitHub's
security tab, can identify which versions receive fixes and exactly how to report privately, and
can see that no response timeframe is promised anywhere in the document.

**Acceptance Scenarios**:

1. **Given** `SECURITY.md`, **When** a researcher reads it, **Then** it names the supported
   versions, gives a private reporting route, and states that reports are read and answered as
   capacity allows, with no timeframe committed.
2. **Given** `SECURITY.md`, **When** a researcher wants to report, **Then** they are directed
   to GitHub's private vulnerability reporting for this repository as the only route, no email
   address is published, and a reporter who cannot use advisories is told to ask for a private
   channel in a public issue without including any vulnerability detail.
3. **Given** `SECURITY.md`, **When** spec 144 later adds the threat model, **Then** a clearly
   marked place already exists for the architecture and trust-boundary pointers, so that work
   adds to the file rather than restructuring it.

---

### User Story 4 - The maintainer can prove the repository is safe to publish (Priority: P3)

Before flipping the repository public, the maintainer needs two pieces of evidence: that no
dependency shipped with coordinare carries license terms incompatible with distributing it
under ELv2, and that a deliberate scrub for secrets and internal infrastructure references
has been performed against both the working tree and the git history.

**Why this priority**: This is the gate that prevents an irreversible mistake. It ranks below
the three published documents because it produces internal evidence rather than something a
reader sees, but the repository must not go public before it passes.

**Independent Test**: A reviewer opens the recorded audit in the feature directory and can
see every distributed dependency with its license and a compatibility verdict, dated; and can
walk the scrub checklist top to bottom, each item naming a concrete command or check rather
than an intention.

**Acceptance Scenarios**:

1. **Given** the recorded dependency audit, **When** a reviewer reads it, **Then** every
   distributed runtime dependency of both the daemon and the performer, including transitive
   dependencies, appears with its license identifier and a compatibility verdict, along with
   the date the audit was taken.
2. **Given** the dependency set, **When** the audit is checked, **Then** no dependency
   carrying strong copyleft terms incompatible with ELv2 distribution is present, and any
   weak-copyleft or unusual license is called out with an explicit rationale for why it is
   acceptable.
3. **Given** a future change that adds a dependency with a license outside the accepted set,
   **When** the test suite runs, **Then** it fails and names the offending dependency and its
   license.
4. **Given** the pre-public scrub checklist, **When** the maintainer works through it,
   **Then** every item is a concrete verifiable check covering secrets, credentials, internal
   hostnames and addresses, and git history, and each item records its outcome.
5. **Given** the scrub checklist, **When** it refers to removing internal infrastructure
   references from examples and configuration, **Then** it points at spec 145 as the owner of
   that cleanup rather than duplicating it.

---

### Edge Cases

- **Legitimate uses of the phrase "open source" elsewhere in the tree.** The repository
  already describes third-party tools and models as open source (for example OpenCode in
  `docs/opencode-sdk.md`, and comments about small open-source models in
  `src/coordinare/services/scoring.py`). A guard against the forbidden claim must catch text
  asserting that *coordinare* is open source, and must not flag accurate statements about
  other people's software. A blanket ban on the phrase would be a false positive machine.
- **Internal pull requests must not be scolded.** All first-party development arrives as pull
  requests into `main`. An automation that responds to every pull request would insult the
  maintainers on every change and add noise to every existing workflow run. The response must
  be conditioned on the author's relationship to the repository.
- **Bot-authored pull requests.** The repository already runs a version-sync workflow that
  pushes to pull requests. Automated and bot authors must not trigger the policy response.
- **A fork-based pull request from an organization member.** Relationship, not fork status, is
  the deciding factor, since a maintainer may work from a fork.
- **The audit is a point-in-time snapshot.** A dependency can relicense between releases, so
  the recorded audit carries its date and the enforcement lives in a test that runs on every
  change rather than only in the document.
- **The README's existing broken license link.** `README.md` already points at a `LICENSE`
  file that does not exist; adding the file resolves the broken link, and the licensing
  section replaces the current single-line stub.
- **Private vulnerability reporting may be disabled at the repository level.** If the GitHub
  setting is not enabled when the repository goes public, the documented primary route is
  dead on arrival, so enabling it is part of the work rather than an assumption.
- **A reader who only ever sees the packaged distribution.** Someone installing a built
  artifact rather than cloning should still find the license in the package metadata.

## Requirements *(mandatory)*

### Functional Requirements

**License and copyright**

- **FR-001**: The repository MUST contain a root `LICENSE` file holding the **unmodified**
  Elastic License 2.0 text as published by Elastic. The license body MUST NOT be edited, because
  ELv2 contains no designation fields: its text is generic by construction ("the licensor is the
  entity offering these terms"), unlike the Business Source License, which does carry fill-in
  parameters. Inserting a licensor line into the body would make the license non-verbatim.
  (Corrected 2026-08-27 after fetching the canonical text; the "designations filled in" framing
  originated in issue #196 and described BSL's structure, not ELv2's.)
- **FR-001a**: Identification of the licensor and the licensed work MUST therefore live outside
  the license body: in `NOTICE` (FR-002), and optionally in a short attribution header placed
  above the license text and visibly separated from it. Vivi Dynamics LLC is the licensor and
  coordinare is the licensed work; ELv2's generic wording binds them by virtue of Vivi Dynamics
  LLC being the entity offering the terms.
- **FR-002**: The repository MUST contain a root `NOTICE` file carrying a single copyright
  statement naming **Vivi Dynamics LLC** and identifying coordinare as the licensed work. This is
  the file that performs the identification ELv2's body does not (FR-001a). Per-file copyright
  headers MUST NOT be introduced.
- **FR-003**: The packaging metadata for the distributed Python projects (the daemon, the
  performer, and the in-tree `service_inference` package) MUST declare the license, so a
  built distribution is not published with unset license metadata.
- **FR-004**: The in-tree first-party package at `packages/service_inference` MUST be covered
  by the root license and copyright statement, and MUST NOT carry a separate or conflicting
  license file.

**Public wording**

- **FR-005**: The README MUST state, in plain English and in no more than one short paragraph
  each: what a user may do (run, copy, modify, self-host, including for their own commercial
  work); what a user may not do (provide coordinare to third parties as a hosted or managed
  service, resell it); that future versions may be offered under different terms; and that
  contribution is by feedback and issues rather than pull requests.
- **FR-005a**: The README's statement of the prohibition MUST make two points explicit, because
  both are load-bearing and neither is obvious to a reader skimming the license: that the
  prohibition covers providing access to coordinare's functionality to others, not merely
  reselling copies of it; and that it applies whether or not money changes hands, since a login
  handed to a client for free is still provision of a hosted service. (Added 2026-08-27.)
- **FR-005b**: The README MUST NOT attempt to adjudicate the boundary cases of the prohibition
  (a client engineer given a read-only board view, a client employee embedded in a supplier's
  team, subsidiaries and group companies). It states the rule; `LICENSE` governs. Prose that
  purports to resolve those cases creates a conflicting second source of truth.
  (Added 2026-08-27.) This is a negative requirement with no mechanical test: absence of
  adjudication cannot be asserted, so it is verified at review time against the final README
  wording rather than by the test suite.
- **FR-024**: The README MUST place a commercial-licensing pointer directly alongside the
  prohibition, inviting anyone who wants to offer coordinare to their own customers, or to have
  it operated on their behalf, to enquire about commercial terms. The prohibition MUST NOT
  appear without that adjacent route. The enquiry route MUST NOT be a public issue template,
  because a commercial licensing enquiry is commercially sensitive to the enquirer, and MUST
  NOT publish a personal email address. (Added 2026-08-27.)
- **FR-006**: All documents describing coordinare's own licensing MUST use the term
  "source-available" and MUST NOT claim that coordinare is open source or OSI-approved.
- **FR-007**: An automated check MUST fail if any tracked document claims that coordinare
  itself is open source or OSI-approved, and MUST NOT fail on accurate descriptions of
  third-party software or models as open source.
- **FR-026**: No document in the posture set may state or imply a warranty, a support
  obligation, or a commitment to respond within any timeframe. This MUST be enforced by the same
  file-scoped automated guard that checks the licensing wording, so the property is tested rather
  than merely intended. (Added 2026-08-27.)

  Rationale: coordinare is given away free under a source-available license, and ELv2 already
  disclaims all warranties and limits liability. The legal exposure is therefore closed by the
  license itself; what remains is expectation management, which is purely a wording problem and
  is cheap to get right before publication and awkward afterwards. The closed-contribution model
  compounds the protection: no external party ever acquires a claim on the project, so going
  quiet for a year costs nothing. The only thing that can create an obligation is a document
  promising one.

**Contribution policy**

- **FR-008**: The repository MUST contain a root `CONTRIBUTING.md` that is a policy rather
  than a build-and-test how-to. It MUST state that issues, bug reports, feature requests and
  feedback are welcome and are read, and that they inform the roadmap; MUST NOT promise triage, a
  response, or implementation of any request (decided 2026-08-27 — a promise of work made to
  strangers for free ages badly into a wall of untouched issues, which reads worse than never
  having made it); MUST describe what a useful report
  contains; and MUST state that public pull requests are not accepted, that this is a
  deliberate supply-chain security measure, and that such pull requests will be closed
  unmerged. Its tone MUST be friendly and MUST NOT apologize for the policy.
- **FR-009**: The repository MUST provide a pull request template that states the
  closed-contribution policy.
- **FR-010**: The repository MUST provide separate issue templates for a bug report, a
  feature request, and general feedback.
- **FR-011**: An automation MUST post a polite policy response linking `CONTRIBUTING.md`,
  apply a label marking the pull request as an external contribution, and then close the pull
  request, when the pull request's author is outside the organization. All three actions happen
  without maintainer involvement, so the policy stated in `CONTRIBUTING.md` (closed unmerged)
  is what actually happens, and the label keeps refused pull requests queryable afterwards.
  (Resolved 2026-08-27.)
- **FR-012**: That automation MUST NOT respond to pull requests authored by organization
  members, maintainers, or bots, and MUST NOT alter the behavior or required status of any
  existing continuous-integration workflow.
- **FR-025**: The automation MUST NOT close an external submission that appears to be
  security-related. Such a submission is commented on and labelled, then left open for a human
  to judge. The detection MUST fail towards human review: a false positive costs one manual
  close, whereas auto-closing a genuine vulnerability report is the outcome this requirement
  exists to prevent. Private vulnerability reports are outside this automation's reach
  entirely, since GitHub advisories are neither issues nor pull requests and no workflow
  observes them. (Added 2026-08-27.)
- **FR-013**: The automation MUST be authored so that it cannot be influenced by the contents
  of the pull request it is responding to, since the pull request author is untrusted.

**Security reporting**

- **FR-014**: The repository MUST contain a root `SECURITY.md` stating its support posture
  (explicitly: no release carries a support commitment, and fixes land on `main` when they land),
  a private reporting route, and an explicit statement that reports are read and responded to as
  capacity allows. It MUST NOT state or imply any response timeframe (decided 2026-08-27):
  coordinare is provided free of charge with no support contract, so publishing a figure would
  manufacture an expectation nobody is owed.
- **FR-015**: `SECURITY.md` MUST direct reporters to GitHub private vulnerability reporting
  for this repository as the sole reporting route. No email address is published. For a
  reporter who cannot use private advisories, `SECURITY.md` MUST instruct them to open a public
  issue requesting a private channel and to include no vulnerability detail in it.
  (Resolved 2026-08-27: no published address, so there is nothing that can bounce or attract
  scanner traffic.)
- **FR-016**: GitHub private vulnerability reporting MUST be enabled on the repository at the
  visibility flip, and MUST be recorded as an `at_flip` item on the pre-public scrub checklist
  with its manual procedure. It cannot be enabled during this feature: the capability exists on
  public repositories only, and this repository is private (confirmed 2026-08-27). Enabling it
  also requires the GitHub web UI, because the available tokens cannot write repository
  administration settings. This feature's obligation is therefore to record the step and to write
  `SECURITY.md` so its primary route becomes live the moment the flip happens, not to perform the
  enablement. (Narrowed 2026-08-27 after the constraint was discovered during planning.)
- **FR-017**: `SECURITY.md` MUST contain a clearly marked section for the architecture and
  trust-boundary pointers that spec 144 will fill in, so that later work extends the file
  rather than restructuring it.

**Evidence for going public**

- **FR-018**: A dependency license audit MUST be recorded in the feature directory, listing
  every distributed runtime dependency of the daemon and the performer, including transitive
  dependencies, with its license identifier and a compatibility verdict, and carrying the
  date it was taken.
- **FR-019**: No distributed dependency may carry license terms incompatible with
  distributing coordinare under ELv2. The accepted set is (decided 2026-08-27):
  - **Permitted without comment**: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0,
    Unlicense, Zlib, and any dual or multi-license expression offering at least one of these.
  - **Permitted as a named, individually justified exception**: MPL-2.0 and the LGPL family.
    Each such package MUST carry its own written rationale in the audit, recording that it is
    redistributed unmodified and how it is consumed (imported as a library, or invoked as a
    separate process).
  - **Rejected**: the GPL family, AGPL, SSPL, and any package whose license metadata is absent
    or unreadable. GPL-family terms forbid adding restrictions, and ELv2's hosted-service
    limitation is exactly such a restriction, so the conflict is real rather than theoretical.
- **FR-019a**: The audit MUST record, for each exception, whether the package is imported into
  coordinare's process or invoked as a separate executable, because that distinction changes what
  the license reaches. A separately invoked executable is aggregation rather than a combined
  work. (Added 2026-08-27.)
- **FR-020**: An automated check MUST fail when a distributed dependency's license falls
  outside the accepted set, naming the dependency and its license, so that a future
  dependency addition cannot silently break the audit.
- **FR-021**: A pre-public scrub checklist MUST be recorded in the feature directory covering
  secrets and credentials, internal hostnames and network addresses, and git history, where
  every item names a concrete check and records its outcome.
- **FR-022**: The scrub checklist MUST attribute the removal of internal infrastructure
  references from examples, defaults and configuration to spec 145, rather than duplicating
  that work.
- **FR-023**: The scrub checklist MUST be executed and its outcomes recorded as part of this
  feature, while the act of making the repository public remains outside this feature.

### Key Entities

- **License grant**: The rights coordinare extends to any recipient (run, copy, modify,
  self-host, including commercially) and the limitations it retains (no third-party hosted or
  managed service, no resale), expressed once in `LICENSE` and paraphrased in the README.
- **Copyright statement**: The single assertion that Vivi Dynamics LLC owns the work, which is what
  keeps future relicensing a unilateral decision and is why no contributor agreement is
  needed.
- **Contribution policy**: The rule that issues are welcome and public pull requests are not,
  expressed in `CONTRIBUTING.md` and enforced at the pull request template and the automation.
- **Author relationship**: Whether a pull request author is an organization member, a bot, or
  an outsider. This is the only input that decides whether the policy response fires.
- **Dependency license record**: One distributed dependency, its license identifier, and a
  verdict on ELv2 compatibility, with the audit's date.
- **Accepted license set**: The enumerated licenses under which a distributed dependency may
  ship, which the automated check enforces.
- **Pre-public scrub item**: One concrete check against the working tree or git history, with
  its recorded outcome.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reader who has never seen coordinare can state its four permissions and two
  prohibitions correctly after under two minutes in the repository root, using only `LICENSE`,
  `NOTICE`, and the README licensing section.
- **SC-002**: Zero tracked documents claim coordinare is open source or OSI-approved, and the
  check enforcing this produces zero false positives against the repository's existing
  accurate references to third-party open-source software.
- **SC-003**: A member of the public who opens a pull request receives the contribution policy
  within one workflow run, with no maintainer action, and every first-party pull request
  receives no such response.
- **SC-004**: A member of the public opening an issue is offered a bug, feature, or feedback
  template rather than a blank box.
- **SC-005**: A security researcher can locate the private reporting route from the repository
  root in one step, and can see that no response timeframe is promised. The route is live from the
  visibility flip onward; it cannot be live while the repository is private (FR-016), so
  liveness is verified at the flip and not before.
- **SC-006**: One hundred percent of distributed runtime dependencies, transitive included,
  appear in the dated audit with a license and a verdict, and none is incompatible with ELv2
  distribution.
- **SC-007**: Adding a dependency whose license falls outside the accepted set fails the test
  suite, and the failure message names the dependency and its license.
- **SC-008**: Every pre-public scrub item marked `now` is recorded as executed with an outcome
  before the feature is considered done. Items marked `at_flip` are explicitly excluded from the
  done definition and are instead enumerated as outstanding, so a fully ticked checklist never
  implies a readiness the repository has not reached.
- **SC-009**: No existing continuous-integration workflow changes its required status, its
  triggers, or its runtime as a result of this feature.

## Assumptions

- The four settled decisions from 2026-08-07 (ELv2 as the license, Vivi Dynamics LLC as sole
  copyright holder, no contributor agreement, closed public pull requests) are inputs and are
  not reopened here.
- `LICENSE` and `NOTICE` name the licensor and copyright holder as **Vivi Dynamics LLC**, the
  official business entity (confirmed 2026-08-27). These two files are legal documents, so they
  carry the entity name rather than the "ViviDynamics" trade style used for the GitHub
  organization and in informal prose.
- The copyright statement carries 2026 as its year, matching the repository's first public
  release rather than the date of first commit.
- **No release carries a support commitment** (decided 2026-08-27). `SECURITY.md` states that
  fixes land on `main` when they land, and that no version, including the latest, is under a
  support obligation. "Fixes land on version X" is itself a commitment, so the pre-1.0 framing it
  replaced was the same trap in a smaller font.
- **No response timeframe is published at all** (decided 2026-08-27). `SECURITY.md` states that
  reports are read and answered as capacity allows, and commits to no figure for acknowledgment,
  assessment, or fix. Coordinare is given away free under a source-available license with no
  support contract, so nobody is owed a response time, and publishing a number would manufacture
  an expectation for nothing in return. Saying plainly that the channel is real while promising
  no schedule is the honest position, and it is enforced by test rather than left to wording
  drift.
- Enforcement checks live in the existing unit test suite under `tests/unit/`, so they run on
  every change through the existing continuous-integration workflows without new
  infrastructure.
- The dependency audit covers what coordinare distributes and runs, not development-only
  tooling, since development dependencies are not conveyed to recipients.
- `semgrep` is not a declared Python dependency of either project. It is installed into the
  performer image by `agent/performer/Dockerfile.full` and invoked as a separate process by
  `src/coordinare/services/security_scanner.py`. It is therefore in scope for the audit as
  redistributed content of that image, and out of scope as a combined work.
- "Outside the organization" is determined by the author's relationship to the repository as
  GitHub reports it, not by whether the branch lives in a fork.
- Making the repository public, the threat model document (spec 144), the removal of internal
  infrastructure references (spec 145), and dashboard authentication (spec 143) are all
  outside this feature.
- Refused external pull requests are commented on, labelled, and closed automatically
  (decided 2026-08-27). The label exists so the refused set stays queryable, since a bug an
  outsider tried to fix is still a bug worth triaging.
- No security email address is published (decided 2026-08-27). Private advisories are the only
  route, which means enabling that repository setting (FR-016) is load-bearing rather than
  merely nice to have.
- The commercial-licensing enquiry route (FR-024) is `https://vividynamics.com/contact`
  (confirmed 2026-08-27, verified reachable the same day) rather than a GitHub issue or a
  published address.
  A public issue template is explicitly unsuitable: a firm exploring commercial terms will not
  announce that negotiation in public, so an issue-based route would suppress the very enquiries
  the pointer exists to attract.
- The closed-contribution policy is load-bearing for two independent reasons, not one. It is a
  supply-chain security measure, and it is also what keeps Vivi Dynamics LLC the sole copyright
  holder. Accepting one outside pull request without a contributor agreement would leave a third
  party holding copyright in part of coordinare, ending the ability to relicense unilaterally.

## Out of Scope

- The threat model document and the dashboard hardening fixes (spec 144, issue #198).
- Removing internal addresses, hostnames, gateway URLs and internal model identifiers from
  examples, defaults and committed routing tables (spec 145, issue #199). This feature owns
  the checklist that says it must happen, not the cleanup.
- Dashboard authentication and authorization (spec 143, issue #197).
- Flipping the repository to public, and any announcement or marketing surface outside the
  repository.
- A contributor license agreement, which the closed-contribution model makes unnecessary.
- Per-file copyright headers.
- Forwarding external issue submissions to a private destination for review (decided
  2026-08-27, tracked as spec 152 / issue #210, which records the settled design). This feature ships issue templates whose structure
  makes that forwarder straightforward to add later, and states nothing in `CONTRIBUTING.md`
  that promises a delivery mechanism it does not have. Triage judgment stays with coordinare's
  existing advocate role rather than moving into a workflow.
