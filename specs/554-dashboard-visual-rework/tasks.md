# Tasks

## Preparation
- [x] T001 Record approved scope, palette, and layout (FR-01, FR-03, FR-08, FR-11).
- [x] T002 Prepare isolated worktree/container preview (FR-10).
- [x] T003 Run speckit.analyze before implementation (all requirements).

## Behavior and appearance
- [x] T004 Write and run failing shipped-JS preference tests (FR-05, FR-06).
- [x] T005 Implement inline appearance lifecycle and accessible selector (FR-05, FR-06).
- [x] T006 Add contrast checks for both themes; implement semantic brand palettes (FR-01, FR-02, FR-07, FR-09).
- [x] T007 Implement approved shell, workspace, typography, responsive navigation (FR-03, FR-04, FR-08).
- [x] T008 Retheme workflow and dynamic component states (FR-07).

## Review and validation
- [ ] T009 Verify HTML budget, affected tests, repository suite, lint/type checks (SC-01, SC-04, SC-05, SC-06).
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
