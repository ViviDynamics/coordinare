# Tasks

## Preparation
- [x] T001 Record approved scope, palette, and layout (FR-01, FR-03, FR-08, FR-11).
- [x] T002 Prepare isolated worktree/container preview (FR-10).
- [x] T003 Run speckit.analyze before implementation (all requirements).

## Behavior and appearance
- [x] T004 Write and run failing shipped-JS preference tests (FR-05, FR-06).
- [x] T005 Implement inline appearance lifecycle and accessible icon buttons (FR-05, FR-06).
- [x] T006 Add contrast checks for both themes; implement semantic brand palettes (FR-01, FR-02, FR-07, FR-09).
- [x] T007 Implement approved shell, workspace, typography, responsive navigation (FR-03, FR-04, FR-08).
- [x] T008 Retheme workflow and dynamic component states (FR-07).

## Review and validation
- [x] T009 Verify HTML budget, affected tests, repository suite, lint/type checks (SC-01, SC-04, SC-05, SC-06).
- [ ] T010 Browser review of both themes at 375/768/1440px, routes, focus and controls (FR-04, FR-07, FR-09, SC-01, SC-02, SC-03).
- [ ] T011 Present preview and iterate until Jason accepts the design (FR-10, FR-11, SC-07).
- [ ] T012 Final review and repository shipping checks after visual acceptance.

## Added identity scope
- [x] T013 Bundle and serve original mark/mascot, favicon and shared header integration (FR-12).
- [x] T014 Verify image formats/cache responses and wheel asset inclusion (FR-12, SC-06).

Browser review has verified both themes and 375/768/1440px layout using served
HTML with inline copies of the same image bytes and synthetic state in T3.
T3's direct localhost navigation is refusing connections despite container HTTP
checks succeeding. Full native keyboard/screen-reader review remains pending.
The first whole-suite run passed 10,919 tests and failed the template readback
check because the template changed during the run; fresh targeted runs pass.
The fresh run after image integration passed: 10,926 tests, 58 skipped, 107
deselected. Subsequent drawer changes pass all 243 affected dashboard checks,
lint, and strict type checking. Full shipping validation follows final design
acceptance.

## Drawer and copy feedback
- [x] T015 Implement/test persistent desktop icon rail and expanded icons/labels; preserve mobile menu behavior (FR-13).
- [x] T016 Move mascot into navigation, retain mark favicon, and remove promotional copy and redundant kicker (FR-12, FR-14).

- [x] T017 Replace appearance select with System / Light / Dark icon buttons;
  preserve persistence, OS changes, and cross-tab selection.
- [x] T018 Style clickable issue/PR links in readable brass shades.
- [x] T019 Move stream status to the header with text states and last-message time.


## Account and complete screenshot review

- [x] T020 Add verified profile API and accessible top-right account menu (FR-15).
- [x] T021 Implement/test provider logout with local revocation, origin rejection,
  cookie clearing, validated discovery URL, and signed-out page (FR-16).
- [x] T022 Capture 84 viewport screenshots across 21 views at 1440x900 and 390x844
  in both themes; provide filterable gallery and downloadable bundle (FR-17).
- [x] T023 Fix mobile configuration-tab overflow and recapture affected views.

The latest full `make test-all` run passes 10,937 tests, with 58 skipped and 107
deselected. Lint, strict type checking, and wheel build pass. The wheel includes
account.js and the signed-out template. Native T3 renders the source with sample
API responses; direct localhost navigation remains unavailable. All captured
views have no page-level horizontal overflow. The mobile account panel fits,
Escape closes it, and focus returns to the account button. Full native keyboard/
screen-reader review and Jason's final visual acceptance remain pending.


## Mascot drawer and unauthenticated landing

- [x] T024 Replace the separate drawer control with the mascot button; animate its
  baton using the original unchanged image, and preserve reduced-motion behavior.
- [x] T025 Add a public OIDC sign-in landing page with explicit Sign in; test that
  APIs, SSE, and the operator shell remain protected.
- [x] T026 Verify desktop/mobile state, artwork preservation, pre-paint pose, title,
  keyboard semantics, and the packaged shared stylesheet.
- [x] T027 Refresh all screenshot views: 88 captures across 22 views, and record
  the baton motion for PR review from mobile.

Latest `make test-all`: 10,939 passed, 58 skipped, 107 deselected. Lint and strict
mypy pass; the wheel packages shell.css and the common public-auth template.
All refreshed captures fit their viewports, and the native T3 recording captures
actual baton transitions with the artwork enlarged for review.
