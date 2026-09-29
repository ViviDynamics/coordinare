---
name: coordinare-conventions
description: Use when starting work in any Vivi Dynamics repository, when unsure which workflow skill applies, or when asked how we ship code, watch CI, or merge here.
license: Proprietary
compatibility: Any agent that can run bash, gh, jq and git.
metadata:
  version: "1.0.0"
  owner: Vivi Dynamics
---

# How we work at Vivi Dynamics

Workflow skills are shared across repositories from the `ViviDynamics/skills`
repository. Repo-specific facts live in the consumer's `repo.env`; skills never contain
them. Scripts do the mechanical work and print JSON; you make the judgment calls the
skill names, and you cite the script output for every claim.

**Where the scripts are.** `S` is a skill's `scripts/` directory, next to its SKILL.md.
Set it once per session, for example `S=.agents/skills/<name>/scripts` (or
`.claude/skills/<name>/scripts` where that is the installed copy), then run every script
as `$S/<script>`.

## Which skill, when

| You want to | Use |
| --- | --- |
| Know the CI rules before touching a run | `ci-safety` (read first) |
| Wait for a PR's CI, retry safely, find out why it failed | `watch-ci` |
| Watch the default branch's CI and retry flakes | `watch-ci-main` |
| Merge a PR, or decide whether it can merge | `merge-pr` |
| Bring a branch up to date with the default branch | `rebase-main` |
| Run a GitHub Copilot review loop on a PR | `copilot-review` |
| Take an issue to a merged PR unattended | `ship-issue` |
| Work an issue to an open PR with a plan gate | `work-issue` (future) |

## Ground rules

1. Every `gh` call in a skill goes through the skill's scripts, either a purpose-built
   script or `$S/vgh` for one-off reads. The scripts resolve the token from `repo.env`
   (`COORDINARE_GH_TOKEN_FILE`), then `~/.gh_token`, then the org token file. Each
   shell call is fresh; scripts handle that for you.
2. A claim needs a read-back. "Merged" means `merge-verified` printed `merged`.
   "Retried" means `verified-rerun` exited 0. "Assigned" means the assignee list read
   back non-empty.
3. State for a long chain lives in `.agents/state/<issue>.json` via `$S/state`.
   Read it before deciding, write it after acting. It is gitignored.
4. Never force-push except inside `rebase-main` on your own unmerged branch, and then
   only with a lease. Never push to the default branch.
5. Never disable, skip or delete a test to make CI green.
6. Prose we publish (PR bodies, comments, docs) uses no em dashes.

## Model tiers (guidance for whoever routes work)

| Stage | Tier |
| --- | --- |
| Resume detection, board and assignee updates, PR creation, CI watch, merge | cheapest available |
| Flake vs real classification with a signatures file | mid |
| Reading the issue, planning, implementing | mid or top |
| Root-causing a real CI failure; code review | top |

Skills do not select models. They are written so the cheapest tier can follow them.

## Adopting these skills in a repo

See `ADOPTING.md` in the skills repository: create `repo.env`, ignore `.agents/state/`,
run `$S/check-wiring` in CI, delete hand-copied skills.
