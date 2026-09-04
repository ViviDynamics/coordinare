# Tasks: Forward External Issue Submissions

**Issue**: #210 | **Branch**: `152-forward-external-issues`

**Guardrails**: do not change any existing workflow's triggers, required status, or runtime; no
destination value in a tracked file; no push or PR without approval.

## Phase 1: Tests first (BLOCKS Phase 2)

- [x] T001 Create `tests/unit/test_152_forward_external_issues.py` with a synthetic issue payload builder. Verify it fails (no script yet).
- [x] T002 [P] FR-003/SC-003: every extracted field is byte-identical to the source, including a title with markup and a non-Latin body. Verify FAILS.
- [x] T003 [P] FR-004: a long body is truncated at the limit and the truncation is visible in the output. Verify FAILS.
- [x] T004 [P] FR-005: body text that imitates instructions or the destination's markup cannot alter the message structure or be read as the workflow's own words. Verify FAILS.
- [x] T005 [P] FR-007/FR-008/SC-005: no destination configured raises; one of two configured delivers to that one and reports the other skipped. Verify FAILS.
- [x] T006 [P] FR-009: a destination that rejects delivery fails and names it. Verify FAILS.
- [x] T007 [P] FR-007: a whitespace-only secret is treated as absent, not as a destination. Verify FAILS.
- [x] T008 [P] FR-006/SC-004: no destination-shaped value appears in any tracked file.
- [x] T009 [P] FR-001/FR-002/FR-010/FR-011: workflow shape — triggers on `issues: [opened]` only, runs on a GitHub-hosted runner, performs no checkout, gates on membership, and leaves every existing workflow's triggers and `build-success` gate untouched.

## Phase 2: The script

- [x] T010 Write `.github/scripts/forward_external_issue.py`: build the extract from the event payload, render per destination, deliver through an injected sender. Makes T002-T004 pass.
- [x] T011 Destination resolution: absent or whitespace-only secrets skipped and reported; none configured raises; a rejection fails naming the destination. Makes T005-T007 pass.

## Phase 3: The workflow

- [x] T012 Write `.github/workflows/forward-external-issues.yml`: `issues: [opened]`, `ubuntu-latest`, no checkout, membership gate mirroring spec 142's permission-level authority, secrets passed as env, script invoked. Makes T009 pass.
- [x] T013 [P] Assert the membership gate uses the permission-level API rather than only the cheap author-relationship field — the under-reporting that scolded a maintainer under spec 142.

## Phase 4: Documentation and verification

- [x] T014 Note in CONTRIBUTING.md that submissions are seen by maintainers, so the closed-PR policy does not read as "nobody is listening".
- [x] T015 Full suite the way CI runs it (bare `pytest`) plus `make lint`.
- [x] T016 Acceptance re-read against SC-001..SC-006; record anything not literally met.
- [x] T017 Scope check; commit with `Closes #210`.

## Phase 5: Review (mandatory)

- [x] T018 Adversarial `Workflow` review over the full branch diff before merge, per the repo's issue workflow. Lenses to include: injection through issue text, secret leakage into logs, membership-gate bypass, and test honesty.
  - **Done 2026-09-04, and it found two live defects in already-merged code.** Spec 152
    shipped as `936c442` (PR #229), so the review ran against `main` rather than a
    pre-merge branch. Six findings survived refute-oriented verification: labels were
    interpolated unescaped into Slack's mrkdwn framing (a label named `<!channel>` pinged
    everyone; `<https://evil/|github.com>` rendered as a trustworthy link, in the
    forwarder's own voice), and `html_url` had no scheme validation, so a `javascript:`
    or `data:text/html` value landed live in an email `href`.
  - The other four findings were test gaps, and they are **why the Slack defect survived
    the earlier review**: the label-escaping test only covered the email renderer, and
    the injection parametrize only ever varied `body`. Both closed.
  - Fixed in PR #254. The mrkdwn framing now carries only our own words plus the issue
    number; labels and author moved to `plain_text`, applying this function's own
    documented lesson that a delimiter with no meaning beats escaping every variation.
  - Note for the next reviewer: the lens that earned its keep was **test honesty**, and
    the decisive step was mutation testing. One of my own fixes (mrkdwn-escaping the URL)
    initially passed every test with the escaping removed — an unpinned guard I would
    otherwise have believed was covered.
