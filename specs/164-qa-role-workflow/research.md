# Phase 0 Research: Spec 164

All items below were open at the end of design. None are tagged
`NEEDS CLARIFICATION`; each is a decision with a recorded rationale, so
Constitution Principle V does not block implementation.

## R1 — Step runner: plain async functions, not langgraph

**Decision**: sequence steps with plain async functions and an explicit state
object. Do not add langgraph to the performer.

**Rationale**: coordinare uses langgraph for a long-lived graph with checkpointing
across daemon restarts. A QA workflow run is one-shot, single-process, and dies
with the container, so checkpointing, resumption and human-in-the-loop
interrupts — the bulk of what langgraph provides — are unused. Constitution
Principle I requires every dependency to be justified, and "consistency with
coordinare" does not justify importing a graph runtime to call six functions in
order. Image weight is explicitly *not* the argument (the image already carries
Chromium, Node and Ruby); the argument is unused surface area.

**Alternatives considered**: langgraph, for one mental model across coordinare and
the performer. Rejected for the above, but the cost of reversing is low: the step
signatures are the design, and the runner is ten lines either way. Revisit if a
workflow ever needs mid-run suspension.

## R2 — Workflow selection and transport

**Decision**: a role config gains an optional `workflow: <name>` alongside the
existing `backend:`. The name travels to the performer in the existing job
payload. The performer resolves it against a registry in
`performer/workflows/__init__.py` that mirrors `SUPPORTED_BACKENDS`.

**Rationale**: the backend registry already solves exactly this problem — a
named, swappable strategy chosen by operator config and validated at load time.
Spec 161 added `SUPPORTED_BACKENDS` as a module-level constant specifically so a
typo could be rejected before dispatch rather than mid-run; the workflow registry
inherits that property for free.

**Alternatives considered**: inferring the workflow from the role name. Rejected:
it removes the operator's ability to run a role the old way, which is the entire
rollout strategy (FR-005).

## R3 — Observation identity *(revised in the second review round)*

**Original decision**: pool repeated model descriptions on `(kind,
document_position)`, reading labels from the DOM, never from the model.

**Superseded**: observation now reads the DOM directly (`workflows/qa/dom.py`);
the model-description pooling path was never wired in and was removed as dead
code. Two lessons survive in `observe._diff_key`. First, the original one --
never key anything on model-supplied labels. Second, one the pooling rule
itself caused when applied to the before/after DIFF: keying on position made an
inserted field look like every element below it had been removed (a false
failure), while ignoring labels made a password field replaced by a workspace
field at the same position look unchanged (a false pass -- the worst outcome
this feature exists to prevent). The diff identity is therefore `(kind, label)`,
with `position` breaking ties only among unlabelled elements of one kind.

**Rationale**: measured during design. Three description runs saw the same new
dropdown and named it `workspace`, `workspace / acme hq`, and `acme hq`;
requiring two identical labels kept zero of them, and the feature under test
vanished from the description entirely — turning a healthy change into an
apparent no-change. Labels are exactly the thing the DOM already knows, so
asking the model for them and then voting on the answer is both unreliable and
unnecessary.

**Alternatives considered**: fuzzy label matching (edit distance, embedding
similarity). Rejected as an approximation of information available exactly, for
added dependency and tuning surface.

## R4 — Baseline checkout mechanics

**Decision**: `git worktree add` at the merge-base, into a sibling directory
inside the container. Do not re-clone, and do not check out over the working
tree.

**Rationale**: the head worktree must stay intact — the workflow needs both trees
alive at once to compare them, and a destructive checkout would strand any
uncommitted state. A worktree shares the object store, so it costs no additional
fetch.

**Alternatives considered**: a second full clone (wasteful, and needs credentials
again); `git stash` plus checkout plus restore (destructive, and fails badly if a
step raises in between).

## R5 — Token budgets

**Decision**: judgment steps ≥ 3000 `max_tokens`, observation steps ≥ 1500.
Retry once at double the budget on `finish_reason: length`. Cap a run at 12 model
calls.

**Rationale**: measured. A judgment call truncated mid-JSON at 500 and completed
cleanly at 3000, with reasoning consuming the difference. This is coordinare #244's
failure mode: a truncation that reaches the caller as empty content and gets
misclassified as malformed output. The retry must be keyed on `finish_reason`,
not on a parse failure, so the two causes stay distinguishable.

**Alternatives considered**: one global budget for all steps. Rejected — an
observation step does not need a judgment step's headroom, and oversizing every
call multiplies gateway contention on a single self-hosted model serving ten
roles.

## R6 — `qa_findings` shape

**Decision**: mirror `scanner_findings`. A list of dicts carrying at minimum
`file`, `line`, `category` and `severity`, so the existing dedup key
`(file, line, category)` in `monitor_performer.py:3465` applies unchanged, plus
QA-specific fields (criterion, failed step, command, exit code, output excerpt,
observed vs expected, repro command).

**Rationale**: spec 083 already built and proved this channel end to end —
produced in `dispatch_performer.py:1355-1357`, carried in `card_context`,
re-merged into the response on the way back. Matching the shape means QA findings
flow through machinery that already exists and is already tested.

**Alternatives considered**: a bespoke QA-only structure. Rejected: it would need
its own carrier, its own dedup, and its own merge path, for no benefit.

## R7 — Failing closed

**Decision**: when a workflow step cannot run, emit a synthetic finding and fail
closed rather than omitting the check.

**Rationale**: `_run_security_floor` already does exactly this — any diff-fetch or
scanner error yields a synthetic `scanner_unavailable` critical finding so the
gate fails closed. QA's historical failure is the opposite (confident passes on
unverified work, which is why spec 120 exists), so inheriting the fail-closed
posture is the conservative and consistent choice. It also composes with the
existing `environment_error` escape hatch, which stays the honest "could not
verify" signal.

## R8 — Scenario fixture generation

**Decision**: generate fixture repositories from a YAML manifest into a temp
directory at eval time. Store the manifests and file contents in the repo; never
commit a `.git` directory.

**Rationale**: a nested git repository inside this repository is a hazard for
clones, tooling, and CI checkout. Generating also makes the base→head commit
relationship explicit and reviewable as data rather than as opaque history.

**Alternatives considered**: git bundles committed as binaries (opaque to review,
and still awkward to regenerate); submodules (heavier, and wrong for fixtures).
