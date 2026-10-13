# Dashboard visual rework: design review

Issue: https://github.com/ViviDynamics/coordinare/issues/94
Owner: Codex, with Jason Berry. GitHub assignee: Jason733i.
Branch: `094-dashboard-visual-rework`, created from main.

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
Dark direction awaiting feedback: green-tinted charcoal or deep forest canvas,
forest surfaces, ivory text, muted-on-dark secondary text, restrained brass.
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
- Awaiting initial palette feedback before dashboard styling changes.
