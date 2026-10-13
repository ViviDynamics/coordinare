# Dashboard visual rework: design review

Issue: https://github.com/ViviDynamics/coordinare/issues/94
Owner: Codex, with Jason Berry. GitHub assignee: Jason733i.
Branch: `554-dashboard-visual-rework`, created from main.

## Agreed workflow

This is a substantial visual rework covering layout, navigation, hierarchy,
typography, spacing, component styling, and brand-aligned light and dark modes.
Keep the branch open for iterative review until Jason accepts the result.
Present the existing brand palette and collect feedback before styling work.
Complete the feature specification, plan, tasks, and speckit.analyze before
implementing the dashboard rework.

## Source identity

Landing project commit: `e63b9317ca314acbe6c9771c1d68957204090f50`.
Source files: `assets/css/tokens.css`, `assets/css/site.css`, `brand.html`.

| Color | Hex | Role |
| --- | --- | --- |
| Forest | #214E3D | Identity and primary actions |
| Ivory | #F6F3EB | Canvas and text on dark |
| Paper | #FFFEFB | Light surfaces |
| Brass | #BC9654 | Decorative accents |
| Ink | #243C32 | Reading text |
| Deep forest | #163B2C | Dark sections |
| Dark charcoal | #14241E | Green-tinted dark foundation |

Light direction: ivory canvas, paper surfaces, ink text, forest actions.
Dark direction confirmed by Jason: deep forest `#163B2C` canvas for a greener
appearance, forest `#214E3D` surfaces, ivory `#F6F3EB` text, muted-on-dark
`#B9CABB` secondary text, and restrained brass `#BC9654` accents.
The source website is fixed-light; a full dark dashboard theme extends it.

## Isolated preview

Dashboard: http://localhost:18094/
Brand guide: http://localhost:18094/brand.html#color
Container: `coordinare-dashboard-94-preview`.
Port mapping: loopback `18094` to container `8090`.
Worktree: `/Users/jasonberry/Workspaces/ViviDynamics/worktrees/coordinare-94-dashboard-visual-rework`.

The initial preview serves the existing dashboard shell with synthetic state
and the original landing brand guide. The conductor is not running in this
preview. Template files are mounted read-only from this branch and read on
each request, so refresh picks up subsequent edits.

The initial preview runner is a local development artifact at
`coordinare/.agents/state/94/preview/server.py`, mounted into the container.
It uses the cached `python:3.12-slim` image and Python's standard library.
No live configuration, credentials, or Docker socket are mounted.

Useful commands:

```sh
docker logs coordinare-dashboard-94-preview
docker restart coordinare-dashboard-94-preview
docker stop coordinare-dashboard-94-preview
```

## Review status

- Issue created and ownership recorded.
- Dedicated worktree and branch created from main.
- Container preview created on a verified unique port.
- Jason selected the deep-forest dark canvas.
- Jason approved the proposed board-first layout; first preview implementation is authorized.

## Approved first-preview layout

Rework existing dashboard views without changing their routes or state contracts.
Use persistent left navigation on desktop, collapsed navigation on small screens,
and a compact top bar with Light / Dark / System appearance selection.

Prioritize active work: performer status and open questions above a wide board,
with activity in an adjacent panel. Keep metrics and system health below the
workspace. Move the detailed workflow diagram out of the primary viewing area
while keeping it accessible. On small screens, stack board and activity.

Use the landing serif for the Coordinare wordmark, system sans for operator
controls and reading, and monospace for logs, code, and identifiers. Use the
confirmed deep-forest/ivory palettes and restrained brass detailing.

Alternative for review: prioritize the activity stream as the main workspace
and place the board beside it. Recommended direction is board first because
card ownership, progress, blockers, and review state are easier to scan together.

Validate both appearances, keyboard navigation, responsive layout, contrast,
and existing dashboard behavior in the isolated container preview.

## Original identity assets

Jason requested the landing mark as favicon and the original conductor mascot
initially featured in the header; later feedback moved it into navigation.
Both are now bundled under dashboard_static, using the
original SVG and WebP bytes. The mascot stays visible in both appearances and at small widths. Jason then
requested moving it from the workspace header into the top-left navigation,
keeping only the mark favicon, and removing promotional copy. This feedback is
implemented. The desktop drawer now retains icons when narrowed to 80px and
shows icons with labels when expanded to 224px. Collapse is saved locally;
mobile opening is independent.

### Header and link refinement

Jason requested System / Light / Dark iconography, brass issue/PR links, and a
more useful system-status indicator in the header. The controls use a monitor,
sun, and moon with a selected background, accessible labels, and tooltips.
Issue/PR links use the theme's brass text shade to maintain 4.5:1 contrast on the
workspace, panels, and PR pills. The relocated status indicator names the stream
state and reports the last received message time on hover. It becomes Live only
after receiving a message and announces status changes rather than heartbeats.


### Account and screenshot review

The header now includes verified user name/initials and an account popup with
email and an OIDC Sign out form. Logout first ends the dashboard session, then
uses provider discovery for RP-initiated logout. The signed-out page does not
automatically restart login. Shared-token and local accounts are labelled
separately. Provider post-logout redirect registration is documented in
`docs/security/dashboard-auth.md`.

The local review gallery at `http://localhost:18094/review/index.html` contains
84 viewport screenshots covering 21 views in desktop/mobile light/dark variants,
including all seven Config tabs, card/performer/symphony details, expanded/
collapsed navigation, account popup, assistant, and signed-out page. PNGs and a
ZIP remain under the ignored preview state directory. Captures use the branch's
rendered HTML/JavaScript and synthetic API data in T3 Browser. The Config tab
strip now scrolls within its panel instead of widening mobile pages.
