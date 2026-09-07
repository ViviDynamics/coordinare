# Feature Specification: Documenter Workflow for a Living Wiki That Humans and Agents Can Read

**Feature Branch**: `171-documenter-workflow`
**Created**: 2026-09-07
**Status**: Draft
**Input**: User description: "Extend the spec-164 role-workflow layer to the documenter (tech_writer) performer: a bounded, code-driven workflow for maintaining the living docs/wiki that models the best practices for documentation serving both human readers and coding agents. The page set is chosen by code from the documentation brief and the diff, each page gets one schema-guarded write with a no-change option, a gate verifies citations, links, scope and a page contract, and code makes one commit. Both documenter modes, update and init, under a page cap. Default off via workflow: documenter."

## Problem

The documenter is a prose plan call (which pages to write or retire) followed by one prose write call per page and a batch commit. Nothing checks that a page's claims point at files that exist, that a page was worth touching, that the wiki stays navigable from its entrypoint, or that writes stayed inside the documentation tree. The persona asks for grounding, conservatism and structure discipline in prose, and the live fleet has produced pages that cite modules that do not exist, per-card changelog cruft, thin stubs, and README files that read as essays rather than indexes. Meanwhile the guidance for documentation that coding agents can use has converged: a short entrypoint in the llms.txt shape (one title, one summary blockquote, sections of one-line described links), one kind of documentation per page (Diátaxis: explanation, how-to, reference, decision), every claim linked to a real path, progressive disclosure over pre-loaded prose, decisions in the ADR shape, and agent instruction files kept short and operational with links into the wiki instead of copies of it. None of that is enforced anywhere.

## Goals

- The set of pages a run may touch is decided by data (the documentation brief and the changed files), not by the model, and is capped.
- Every page the run commits satisfies a contract code can check: citations exist, wiki links resolve, one kind per page with that kind's required headings, an entrypoint in the llms.txt shape that links every page exactly once, no changelog lines, size bounds.
- The wiki has a fixed shape a newcomer and an agent can navigate: `docs/wiki/README.md` as the index, section pages typed by kind, decisions as ADR files.
- The agent pointer sections in `AGENTS.md` and `CLAUDE.md` are a short template that links into the wiki, never a copy of it.
- The run commits exactly once, only documentation paths, and reports `docs_committed` with the files it wrote, so coordinare's existing documenting-stage handling is unchanged.
- Without the workflow flag the stage behaves byte for byte as today.

## Non-goals

- Generating diagrams (a later spec may add mermaid where the brief asks for it).
- Documenting code the card did not touch beyond what the brief names (init mode excepted).
- Replacing coordinare's wiki-init gate (spec 124 US2); this spec makes the init run itself bounded.
- Card documentation under `docs/cards/`; the documenter maintains the wiki, the cards folder is the architect's and assessor's.
- Retiring pages the plan did not select.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A card's documentation lands grounded and navigable (Priority: P1)

A card adds a payment module. The architect's documentation brief names one page to write (`docs/wiki/payments.md`, kind reference, what to say) and the diff touches `src/payments/`. The documenter writes that page from the brief and the code, updates the architecture page because it cites `src/`, refreshes the README index so the new page is linked once with a one-line description, and commits once. Every path the pages cite exists; every wiki link resolves.

**Why this priority**: this is the stage's purpose; a page that cites a module that does not exist is worse than no page.

**Independent Test**: run the workflow against the `feature` fixture with a stubbed model; the record shows two pages written and the README refreshed, one commit under `docs/`, every citation found, every link resolved.

**Acceptance Scenarios**:

1. **Given** a brief naming `docs/wiki/payments.md` and a diff under `src/payments/`, **When** the workflow runs, **Then** the plan holds `payments.md` (source brief), `architecture.md` (source inventory, because it cites `src/`) and `README.md` (always, when any page changes), and nothing else.
2. **Given** the model's page content cites `src/payments/ledger.py` which does not exist, **When** the gate runs, **Then** the page is dropped with the missing citation recorded and the other pages still commit.
3. **Given** the model returns `unchanged` for the architecture page with a reason, **When** the gate runs, **Then** no write happens for it and the reason is on the record.
4. **Given** the run wrote pages, **When** it commits, **Then** there is exactly one commit, it touches only documentation paths, and `docs_committed` carries the written paths.

---

### User Story 2 - A trivial card leaves the wiki alone (Priority: P1)

A card fixes a typo in a template. The brief has no docs entries and the diff touches nothing any wiki page cites. The plan is empty, no model call is made, nothing is committed, and the stage reports `docs_committed` with no files, which coordinare already treats as an honest no-op.

**Why this priority**: the documenter runs on every card; noise here pollutes the wiki fastest.

**Independent Test**: run the `trivial` fixture; zero model calls, zero commits, `docs_committed` with an empty file list.

**Acceptance Scenarios**:

1. **Given** no brief docs and a diff touching only uncited files, **When** the workflow runs, **Then** the plan is empty and the run ends after intake with no model call.
2. **Given** a brief with no docs but a diff touching a file the setup page cites, **When** the workflow runs, **Then** the plan holds the setup page and the README, and the model may answer `unchanged`.

---

### User Story 3 - The wiki keeps its shape (Priority: P1)

Over many runs the README stays an index in the llms.txt shape, every page keeps one kind with that kind's headings, decisions stay in the ADR shape, and no page carries per-card changelog lines.

**Why this priority**: shape is what makes the wiki usable by an agent that reads the index first and loads pages just in time.

**Independent Test**: run the `shape` fixture where the model returns a README that is an essay, a how-to page missing its verification heading, and a page with a `## Card #123` heading; all three are dropped with the contract check named, and the README is regenerated by code from the surviving pages.

**Acceptance Scenarios**:

1. **Given** a README write without the summary blockquote or with a page linked twice, **When** the gate runs, **Then** the README write is dropped and code regenerates the index from the page inventory instead.
2. **Given** a page whose frontmatter kind is `how-to` and whose body lacks a `## Verify` heading, **When** the gate runs, **Then** the page is dropped naming the missing heading.
3. **Given** a decision page missing `## Consequences`, **When** the gate runs, **Then** it is dropped naming the heading.
4. **Given** any page with a heading that contains an issue reference like `#123` or the word `changelog`, **When** the gate runs, **Then** it is dropped as a changelog line.

---

### User Story 4 - A repository without a wiki gets a bounded first build (Priority: P2)

A symphony starts against a repository with no `docs/wiki/`. In init mode the plan is a skeleton derived from the repository layout (README, architecture, setup, testing, one reference page per top-level package with tests), capped at 8 pages, each written from gathered code excerpts, gated like any other page, and committed once with the pointer sections added to `AGENTS.md` and `CLAUDE.md`.

**Why this priority**: the init run is the largest single documenter job and the one most prone to invented content.

**Independent Test**: run the `init` fixture against a generated repository with three packages; the plan has at most 8 pages, every written page passes the contract, the README links each page exactly once, and the pointer sections are present and under 40 lines.

**Acceptance Scenarios**:

1. **Given** a repository with five top-level packages with tests, **When** init runs, **Then** the plan holds README, architecture, setup, testing and the first four packages by size, and records the fifth as deferred.
2. **Given** the model's setup page cites a command file that does not exist, **When** the gate runs, **Then** the page is dropped and the README index omits it.

---

### User Story 5 - Agents find the wiki from their instruction files (Priority: P2)

After any run that changed the wiki, `AGENTS.md` and `CLAUDE.md` carry a fixed pointer section: what the wiki is, its entrypoint path, and the three pages to read before changing code. The section is a template filled by code, replaced in place between markers, never written by the model.

**Independent Test**: run the `feature` fixture on a repository whose `AGENTS.md` has a stale pointer section; the section is replaced between markers, is under 40 lines, and the rest of the file is byte for byte unchanged.

---

### Edge Cases

- The diff is truncated: the changed-file list comes from the parsed diff; files beyond the cut are unknown and pages citing them are not selected. Recorded on the run.
- A brief entry names a page outside the documentation paths: refused at plan time, recorded, never written.
- The model returns `retire` for a page the plan selected because of the brief: refused; retirement is allowed only for a page selected from the inventory whose citations no longer exist.
- Two brief entries name the same page: one plan entry, both "say" texts passed to the write.
- The README is the only page in the plan (an index-only refresh): allowed; code regenerates it, no model call.
- The workspace has uncommitted changes from an earlier stage: the run refuses to start and holds (`env_blocked`) rather than sweeping them into the docs commit.
- The commit fails (push rejected): `env_blocked` with the error; nothing is reported as written.
- Init mode on a repository that already has `docs/wiki/`: the init skeleton is merged with the inventory and existing pages are updated, not overwritten blindly.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: documenter` on the tech_writer role, the documenting stage MUST run intake, plan, gather, write, gate, pointers, commit, report, in that order, advanced by code.
- **FR-002** Intake MUST read the documentation brief, parse the injected diff into changed files, take the mode from `doc_mode`, and build the wiki inventory by code: every page under `docs/wiki/` with its kind, its citations (backticked repository paths and link targets that resolve to repository files) and its wiki links.
- **FR-003** The plan MUST be built by code. Update mode: the brief's docs entries (location is the page path, deduplicated) plus every inventoried page whose citations intersect the changed files, plus the README whenever any other page is selected; at most 8 pages, brief entries first, the overflow recorded as deferred. Init mode: README, architecture, setup, testing and one reference page per top-level source package that has a tests directory, largest first, at most 8, merged with the inventory when a wiki already exists.
- **FR-004** A plan entry outside the documentation paths (`docs/`, `doc/`, `README*`, `CONTRIBUTING*`, `CHANGELOG*`, `AGENTS.md`, `CLAUDE.md`) MUST be refused and recorded.
- **FR-005** For each planned page (except the README, which code generates), gather MUST collect through the spec-165 allow-list: the page's current content, the brief's text for it, the diff hunks of the changed files it cites or documents, and the first 200 lines of each module the brief names for it, recorded with every command and refusal.
- **FR-006** Each planned page MUST get exactly one schema-guarded model call with one reprompt, returning `write` with content, `unchanged` with a reason, or `retire` with a reason; the path is fixed by the plan and the schema MUST reject any other field. `retire` MUST be accepted only for a page selected from the inventory whose citations no longer exist.
- **FR-007** The gate MUST re-extract citations and links from the written content and drop a page when any cited repository path does not exist at the branch head or any wiki link does not resolve to a page that exists after the run.
- **FR-008** The gate MUST enforce the page contract: exactly one H1; frontmatter `kind` in `explanation | how-to | reference | decision`; the kind's required H2 headings present (explanation: `What it is`, `How it fits`, `Why it is this way`, `Where to change it`; how-to: `Goal`, `Prerequisites`, `Steps`, `Verify`; reference: at least one table or list with a cited path per entry; decision: `Context`, `Decision`, `Consequences`, `Status`); unique headings with no links or inline code in them; every fenced block with a language; no link text `here` or `link`; at least 400 characters for a non-README page; at most 12,000 characters; no heading or line that reads as a changelog entry (an issue reference like `#123` in a heading, or a heading containing `changelog`).
- **FR-009** The README MUST be generated by code from the surviving page inventory in the llms.txt shape: one H1 with the project name, a blockquote summary under 300 characters (taken from the existing README or the brief summary), H2 sections `Start here`, `Architecture`, `How to`, `Reference`, `Decisions` and `Optional` whose bodies are `- [Title](page.md): one line` entries, every page linked exactly once. A model-written README MUST be dropped in favour of the generated one.
- **FR-010** Decision pages MUST live under `docs/wiki/decisions/` and carry the ADR headings; the README's `Decisions` section MUST list them.
- **FR-011** When any wiki page changed or init ran, the pointer sections in `AGENTS.md` and `CLAUDE.md` MUST be replaced in place between fixed markers with a template under 40 lines naming the wiki entrypoint and the three pages to read first; the rest of each file MUST be unchanged. A file without markers gets the section appended.
- **FR-012** The commit step MUST write only the surviving pages, retirements and pointer sections, revert any other path the run touched, and make exactly one commit `docs(#n): <summary>`; a run with nothing to write MUST make no commit. A dirty tree at start or a failed commit or push MUST end the run as `env_blocked`.
- **FR-013** The performer MUST report `docs_committed` with the written and retired paths, and `env_blocked` for a hold; the init-mode PR open behaviour of `_commit_doc_batch` MUST be preserved.
- **FR-014** Without `workflow: documenter` the documenting stage MUST behave byte for byte as today.
- **FR-015** Every step MUST log its duration and every model call its elapsed time and completion tokens, in the events specs 164 to 170 emit.
- **FR-016** Every gate rule MUST be a pure function with its own test, shown to fail under a mutation, including the page contract checks and the README generator.
- **FR-017** The persona MUST carry the writing rules: one kind per page, every claim cited by path, lead with what a newcomer needs, explicit negative rules and one correct example over prose, plain repeated sentence shapes, no file-by-file narration of what the code already says, record why rather than what changed, link to the canonical home instead of repeating.

### Key Entities

- **WikiPage** (inventory): path, kind, title, citations, links, size.
- **PagePlan**: path, kind, source (brief | inventory | init | index), justification, exists.
- **PageEvidence**: commands run and refused, characters gathered.
- **PageResult**: path, action, reason, citations checked and missing, links missing, contract failures, size, dropped and drop reason.
- **DocsRecord**: mode, brief present, changed files, inventory size, plan and deferred pages, results, files written, files retired, pointers refreshed, commit sha, reverted paths, verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Every committed page cites only paths that exist and links only pages that exist, on all fixtures and the first ten live runs.
- **SC-002** The README passes the llms.txt shape check and links every page exactly once after every run that changes the wiki.
- **SC-003** Zero commits touching a path outside the documentation set.
- **SC-004** A documenting round completes in under ten minutes at the 90th percentile including container start for update mode, and under twenty for init mode, over the first ten live rounds.
- **SC-005** Zero model calls on cards whose plan is empty.
- **SC-006** The six fixtures (`trivial`, `feature`, `shape`, `hallucinated_citation`, `init`, `pointers`) pass deterministically in CI, and `feature` and `init` pass live through the gateway.

### Performance budgets (Constitution IV, provisional until measured live)

- Plan cap: 8 pages per run.
- Gather: 6 commands and 4000 characters kept per command per page.
- Write: one call at 6000 completion tokens plus one reprompt per page.
- Page size: 400 to 12,000 characters.

## Assumptions

- The reviewer's diff parser, allow-list survey and write-free check are reusable as imports.
- `commit_files` in the performer workspace layer accepts writes and deletions and pushes; it is reused for the single commit.
- The spec-165 documentation brief carries `docs` entries with `topic`, `location` and `say`, and `modules`.
- Coordinare's documenting-stage handling (spec 126 noop rule, spec 125 last-documented SHA, spec 124 init PR) works from `files_modified` and needs no change.

## Rollout

Default off. Enable per symphony with `workflow: documenter` on the tech_writer role. First live rounds on a low-traffic symphony; the prose path returns by removing the line.
