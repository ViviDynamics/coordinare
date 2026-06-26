# Coordinare — Developer Onboarding

Welcome. **Coordinare** is an autonomous software-engineering orchestrator: it watches a
GitHub project board, picks up cards, and drives them through a full engineering lifecycle —
plan → implement → review → security → QA → document → merge — by dispatching AI agents
("performers") that run real CLIs in containers against the project's real environment.

This guide explains how the pieces fit together, with an emphasis on the parts that surprise
people: **how one coordinare orchestrates many performers across many projects, how those
performers run different agent harnesses, and how shims make local open models behave like
frontier models.**

## The one-paragraph mental model

> A **coordinare** (one daemon) conducts several **symphonies** (projects, each a GitHub board).
> For each card it dispatches a sequence of **performers** (one per lifecycle role). Every
> performer runs an **agent harness** (a CLI like Claude Code, Codex, opencode, junie…) inside a
> container that has the project's cached dev **environment** mounted. The harness talks to a
> **model** — a frontier API (Claude) or a local open model (gpt-oss, qwen, glm). When it's a
> local model, a **shim** sits in front of it and normalizes/translates its output so the harness
> sees frontier-grade behavior. Gates between stages enforce CI, security, and human approval.

## Read in this order

1. **[01 — Overview & Purpose](01-overview.md)** — what problem this solves, the orchestra metaphor, key terms.
2. **[02 — Architecture](02-architecture.md)** — the layered system, with diagrams (the big picture).
3. **[03 — Performer Lifecycle](03-performer-lifecycle.md)** — roles, stages, gates, the board, the dispatch→monitor loop.
4. **[04 — Harnesses & Shims](04-harnesses-and-shims.md)** — backends, the proxy/shim layer, routing strategies, normalizers, local→frontier parity.
5. **[05 — Configuration Compositions](05-configuration.md)** — `config.yaml` + `routing.yaml` + `.env`, the layered/catalog model.
6. **[06 — Environment Cache](06-env-cache.md)** — bootstrap, activation, services, performer-owned setup.
7. **[07 — Roadmap](07-roadmap.md)** — the spec lines, what's shipped, what's next.

There is also a **visual slide deck** (rendered separately) that condenses this into ~15 slides
for a quick orientation or a presentation.

## Glossary (quick reference)

| Term | Meaning |
|---|---|
| **Coordinare** | The orchestrator daemon. One process, watches all symphonies. |
| **Symphony** | One orchestrated project = one GitHub Projects board + repo + config. |
| **Card** | A unit of work (a GitHub issue on the board) moving through the lifecycle. |
| **Performer** | An AI agent instance dispatched for one role/stage, running in a container. |
| **Role / Stage** | The job a performer does (implementer, reviewer, qa…) and its lifecycle position. |
| **Agent harness / Backend** | The CLI the performer runs (claude_code, codex, opencode, junie, pi, hermes, openclaw). |
| **Model** | The LLM the harness calls — frontier (Claude) or local (gpt-oss:120b, qwen, glm). |
| **Shim / Proxy** | A loopback reverse-proxy that normalizes/translates a local model's wire so the harness sees frontier behavior. |
| **Normalizer** | A pure transform that repairs one local-model output pathology (harmony leak, reasoning leak, control chars). |
| **Env-cache** | The per-symphony cached dev environment (toolchain + activation + services) mounted into performers. |
| **Gate** | A control point between stages (CI gate, security gate, env-blocked, human approval). |

> **Accuracy note:** the spec line is large and active (specs 001–119). Some behaviors below
> reference draft/in-flight specs; where a doc says "spec NNN," that's the source of truth.
</content>
