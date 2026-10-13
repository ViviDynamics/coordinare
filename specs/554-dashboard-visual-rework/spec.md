# 554: Dashboard visual rework

Issue: #94. Owner: Codex. Status: first design iteration approved for implementation.

## Intent and scope

Operators need to scan card progress, blockers, and live activity in a dashboard
that carries the Coordinare landing identity. Jason approved persistent left
navigation, a board-first workspace with adjacent activity, compact metrics,
and both ivory/paper light and deep-forest dark appearances. The branch remains
open until Jason accepts the visual result. Existing routes and API contracts
remain the integration boundary.

## User stories

US1: As an operator, I can see the board, active performers, and questions before
secondary system detail. At desktop widths the board and activity are adjacent;
on narrow screens they stack without losing controls or content.

US2: As an operator, I can choose Light, Dark, or System, and keep that preference
across reloads. System follows OS changes; explicit choices do not. The correct
appearance applies before first paint, even if local storage is unavailable.

US3: As an operator, I can use every existing page and action in both appearances
with readable status labels, logs, charts, keyboard focus, and form controls.

## Requirements

FR-01: Use the exact landing core palette: forest #214E3D, ivory #F6F3EB,
paper #FFFEFB, brass #BC9654, ink #243C32. Dark canvas is #163B2C.
FR-02: Source color, type, spacing, and geometry from shared semantic tokens.
FR-03: Use desktop left navigation, a compact header, the board/activity workspace,
and secondary metrics/health/workflow detail. Preserve existing routes and IDs.
FR-04: Preserve keyboard access and content at 375px, 768px, and 1440px widths.
FR-05: Offer Light/Dark/System, store the choice locally, follow OS changes only
in System, and tolerate invalid preferences and denied storage.
FR-06: Resolve appearance in the document head before styles render; set color-scheme.
FR-07: Apply theme tokens to status labels, inputs, logs, charts, assistant views,
and dynamic components. Retheme the workflow when appearance changes.
FR-08: Use serif for the wordmark, system sans for operator UI, mono for code/logs.
FR-09: Keep explicit status labels, visible keyboard focus, and reduced motion.
FR-10: Maintain an isolated container preview at localhost:18094 using synthetic
state, mounted branch assets, and no conductor execution.
FR-11: Keep iterative work on 554-dashboard-visual-rework; await Jason's visual
acceptance before merging.

FR-12: Bundle the original coordinare-landing mark and conductor mascot in this
package. Use the mark as favicon and the mascot as the navigation identity in both
themes, including small screens. Keep the workspace header free of the mascot. Serve correct image MIME
types and cache validators. Reserve image dimensions before load.

FR-13: Desktop navigation narrows to an 80px icon rail and expands to 224px
with icons and labels. Icons remain usable with accessible names and tooltips.
Remember desktop collapse locally and apply it before paint; mobile navigation
opens independently and retains the desktop preference across viewport changes.
FR-14: Remove added promotional taglines and the redundant workspace kicker.

FR-15: Add a top-right user account menu showing verified OIDC profile claims,
with a labelled avatar and Sign out action. Label local/shared-token access
accurately and keep tokens out of the profile API.
FR-16: Browser logout revokes the dashboard session and clears cookies before
OIDC provider logout. Use a validated HTTPS discovery endpoint and ID-token hint;
return to an explicit signed-out page. Preserve local logout API compatibility.
FR-17: Provide light/dark desktop and mobile screenshots for every operator page,
configuration tab, drilldown view, account menu, and signed-out view.

FR-18: Use the mascot as the only navigation toggle, with a baton pivot tied to
expanded/collapsed state. Preserve the original artwork, keyboard/button semantics,
action labels and saved desktop preference; honor reduced-motion preferences.
FR-19: With OIDC enabled, unauthenticated page visits reach a basic branded sign-in
landing page. Start SSO only on explicit Sign in. Keep APIs/SSE protected and
expose only the public authentication pages and original branding resources.

## Edge cases

Storage can throw; saved values can be invalid; OS preference can change;
chart rendering can race a theme switch; SSE can disconnect; long titles and
logs can overflow; navigation must remain available on small screens.

## Success criteria and budgets

SC-01: Both themes and all existing routes render with no new browser errors.
SC-02: Text and event-chip pairs meet 4.5:1; focus/interactive boundaries meet 3:1.
SC-03: No page-level horizontal overflow at the three target widths; long logs
and wide tables can scroll within their own regions.
SC-04: Appearance state tests cover saved choices, System changes, invalid values,
storage failures, and cross-tab updates using shipped JavaScript.
SC-05: Keep the existing 112KiB dashboard HTML budget, add no external fonts or
product dependencies, and add no server requests to resolve the appearance.
SC-06: Bundle no more than 164KiB of original identity images; use cache validators
for their stable URLs and reserve layout space. Theme resolution stays client-side.
Use cached theme-neutral assets; inline appearance logic runs before the
existing external Mermaid request. Theme preference resolution adds no server startup work.
SC-07: Jason accepts the final design in the container preview before merge.
