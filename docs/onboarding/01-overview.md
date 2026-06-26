# 01 — Overview & Purpose

## What Coordinare is

Coordinare is a **long-running daemon that runs a software team made of AI agents**. You put
work on a GitHub project board; coordinare picks it up and drives each item all the way to a
merged PR — writing a plan, implementing it, reviewing it, security-scanning it, QA-ing it
against a real environment, documenting it, and merging it once a human approves.

It is *not* a single "do my task" agent. It is an **orchestrator** that:

- watches **multiple projects** at once (each is a "symphony"),
- runs a **multi-stage engineering lifecycle** per card (not one shot),
- dispatches **specialized performers** per stage (an implementer, a reviewer, a QA agent…),
- runs those performers as **real CLIs in containers** against the project's **real toolchain**,
- can mix **frontier models** (Claude) and **self-hosted open models** (gpt-oss, qwen, glm),
- and enforces **gates** (CI must pass, security floor, *only humans merge*).

## The orchestra metaphor (why the names)

The naming is deliberate and worth internalizing — it maps cleanly onto the architecture:

| Music | Coordinare | Meaning |
|---|---|---|
| **Coordinare** | the daemon | one coordinator directing everything |
| **Symphony** | a project | a complete work being performed (one repo + board) |
| **Score** | the dispatch payload | what a performer is told to play (card, persona, context) |
| **Performer** | an agent instance | one player executing one part |
| **Harness** | the CLI backend | the *instrument* a performer plays on |
| **Roles** | lifecycle stages | first chairs: planner, implementer, reviewer… |

One coordinare → many symphonies → each symphony's card → a sequence of performers → each on a
harness → each harness playing a model.

## The problem it solves

Writing software with AI agents at scale runs into three hard problems. Coordinare is, in
essence, the accumulated answer to all three:

1. **One shot isn't enough.** Real changes need plan → build → review → test → fix → merge,
   with feedback loops and CI. Coordinare models this as an explicit **lifecycle** with gates,
   bounces, and bounded retries (rather than hoping one prompt produces a mergeable PR).

2. **Agents need a real environment.** Tests need the toolchain, a database, services. Coordinare
   maintains a **per-project env-cache** — the dev environment is bootstrapped once and mounted
   into every performer, with services started on activation.

3. **Open models aren't frontier models — yet you want to use them.** Local models (gpt-oss,
   qwen) leak reasoning channels, mangle tool calls, emit invalid JSON, and speak the wrong wire
   format. Coordinare's **shim/normalizer layer** transparently repairs these so an off-the-shelf
   agent harness — built for Claude or GPT — works against a local model **without modification**.

## What "done" looks like for a card

A card is *done* when its PR is **merged** — and coordinare will only merge after:

- every lifecycle stage completed successfully,
- the **CI gate** is green (required checks pass),
- the **security** stage passed,
- and a **human approved** the PR (bots may comment and request changes, but **only humans
  approve** — a core safety rule).

Everything up to that human approval is automated; the human approval is the deliberate
hand-off point.

## Where to look in the code

| Concern | Path |
|---|---|
| Daemon entrypoint | `src/coordinare/__main__.py`, `src/coordinare/daemon.py` |
| Lifecycle / workflow graph | `src/coordinare/lifecycle.py`, `src/coordinare/graph/` |
| Workflow nodes (the loop) | `src/coordinare/graph/nodes/` (check_board, dispatch_performer, monitor_performer, monitor_pr, closer) |
| Performers / harnesses | `agent/performer/src/performer/` (backends/, proxy/) |
| Config | `src/coordinare/config.py`, `config.yaml`, `routing.yaml` |
| Env-cache | `src/coordinare/services/env_cache.py`, `agent/performer/devenv-profile.sh` |
| Specs (source of truth) | `specs/NNN-name/spec.md` |

Next: **[02 — Architecture](02-architecture.md)** for the big-picture diagrams.
</content>
