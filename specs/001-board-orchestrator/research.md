# Research: Board Orchestrator Daemon

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16
**Status**: Complete

## R1: LangGraph for Daemon-Style Orchestration

**Decision**: Use LangGraph `StateGraph` as the core workflow orchestrator for the coordinare daemon.

**Rationale**: LangGraph provides stateful, cyclical graph execution with built-in checkpointing — exactly what a long-running board-monitoring daemon needs. It supports conditional routing, typed state schemas with reducers, and persistence backends (SQLite for dev, PostgreSQL for production). Unlike DAG-only frameworks, LangGraph explicitly supports the cycles required for the poll-dispatch-monitor-merge loop.

**Alternatives Considered**:
- **Plain asyncio loop with manual state**: Simpler but requires building state persistence, crash recovery, and conditional routing from scratch.
- **Celery/Temporal**: Designed for distributed task queues, not single-daemon stateful workflows. Over-engineered for one-board-at-a-time orchestration.
- **LangGraph Platform (Cloud)**: Managed hosting with cron support, but adds vendor lock-in and cost for a single-daemon use case.

**Key Technical Details**:
- Package: `langgraph` (PyPI), stable release line 0.2.x+
- LangGraph depends on `langchain-core` (lightweight ~3MB), NOT the full `langchain` package
- State schema via `TypedDict` with `Annotated[T, reducer]` for accumulating channels
- Checkpointers: `MemorySaver` (dev), `SqliteSaver` (lightweight), `PostgresSaver` (production)
- Default `recursion_limit=25` per invocation — design graph so one `.invoke()` = one polling cycle
- Compiled graph exposes `.invoke()`, `.ainvoke()`, `.stream()`, `.astream()`, plus `.get_state()` and `.update_state()` for health checks
- All node functions should be `async def` to avoid blocking the asyncio event loop

**Daemon Pattern**: LangGraph is NOT a daemon framework. Wrap the compiled graph in an `asyncio` `while True` loop with `asyncio.sleep(poll_interval)` between invocations. The checkpointer with a stable `thread_id` carries state across invocations, enabling crash recovery (FR-017).

**Gotchas**:
- State size matters — checkpoint I/O scales with state dict size; store IDs not payloads
- `SqliteSaver` is NOT safe for concurrent multi-process access; use `PostgresSaver` for production
- Reducer channels (e.g., `Annotated[list, add]`) accumulate across invocations; clear explicitly at cycle start
- Pre-1.0 API — pin version exactly in `pyproject.toml`
- No built-in scheduling; `time.sleep()` inside nodes blocks the executor

---

## R2: Anthropic Claude SDK for AI Reasoning

**Decision**: Use the `anthropic` Python SDK for all LLM-powered reasoning tasks within the coordinare (card sufficiency analysis, clarifying question generation, PR review summarization).

**Rationale**: There is no separate "Claude Agent SDK" — the `anthropic` package IS the SDK for building agents. It provides tool use, multi-turn conversations, extended thinking, streaming, and prompt caching. For the coordinare's needs (evaluate card clarity, formulate questions, triage reviews), Claude's structured tool-use protocol maps directly to each decision point.

**Alternatives Considered**:
- **`langchain-anthropic` wrapper**: Adds abstraction overhead, sometimes lags behind native SDK features (extended thinking, prompt caching). Not recommended by Anthropic for new projects.
- **`litellm`**: Unified multi-provider proxy. Useful if we need multi-model routing, but adds a dependency layer for no benefit when Claude is the sole reasoning model.
- **No LLM integration**: Possible for pure rule-based orchestration, but FR-012 (insufficient card details) and FR-013 (formulating clarifying questions) strongly benefit from LLM reasoning.

**Key Technical Details**:
- Package: `anthropic` (PyPI), version >=0.40
- Tool use: Define structured `input_schema` for each decision (card assessment, review classification)
- Agent loop pattern: Manual `while response.stop_reason == "tool_use"` loop
- Extended thinking: `thinking={"type": "enabled", "budget_tokens": N}` for complex analysis
- Prompt caching: `cache_control: {"type": "ephemeral"}` on system prompts for cost reduction
- Use Claude narrowly at decision points; keep deterministic logic (board polling, reviewer classification, merge operations) in pure Python

**Integration with LangGraph**: Use `anthropic` SDK directly inside LangGraph nodes. Each node that needs reasoning makes a focused Claude call. LangGraph handles orchestration; Claude provides intelligence.

---

## R3: Codex CLI / OpenCode — Agent Dispatch Targets

**Decision**: "Codex" and "OpenCode" are terminal-based AI coding agent CLIs (peers of Claude Code), NOT SDKs for building multi-model orchestrators. The coordinare dispatches work to these agents via SSH (FR-019), not by embedding their APIs.

**Rationale**: The coordinare's multi-model story is at the **agent dispatch level** — choosing which CLI agent to SSH into per project configuration. Each agent (Claude Code, Codex CLI, opencode) handles its own model provider internally. The coordinare invokes them via CLI commands over SSH and parses their output.

**Alternatives Considered**:
- **Direct SDK integration (anthropic + openai)**: Would tightly couple the coordinare to specific LLM providers. The SSH/CLI dispatch pattern provides better isolation and flexibility.
- **`litellm` as multi-provider proxy**: Useful if the coordinare itself needs to call multiple LLMs, but the spec's architecture (FR-019) delegates code generation to external agents.

**Key Technical Details**:
- Codex CLI: OpenAI's terminal coding agent, reads `AGENTS.md` for project context
- OpenCode: Open-source terminal coding agent, also reads `AGENTS.md`
- Both grouped under `AGENTS.md` convention in the `.specify` tooling
- SSH dispatch pattern: `ssh agent-host "codex run --context card.json"` (exact CLI interface TBD per agent)
- The coordinare only needs `paramiko` or `asyncssh` for SSH connections, not agent-specific SDKs

---

## R4: GitHub Projects API v2 (GraphQL)

**Decision**: Use GitHub's GraphQL API (Projects v2) with the `gql[aiohttp]` Python client for all board operations.

**Rationale**: GitHub Projects v2 is exclusively GraphQL — there is no REST API for project board operations. The `gql` library provides async support (essential for the daemon), schema validation, and clean variable handling. This directly supports FR-018 (GitHub Projects as sole board provider).

**Alternatives Considered**:
- **`sgqlc` (schema-generated client)**: Type-safe but heavier setup and steeper learning curve.
- **Plain `httpx`**: No GraphQL-specific features; manual query string management.
- **`PyGithub`**: REST-only; does not support Projects v2 operations.

**Key Technical Details**:

### Authentication
- **Fine-grained PAT** (dev): `projects:rw`, `issues:rw`, `pull_requests:rw`, `contents:read`
- **GitHub App** (production, recommended): Installation tokens auto-rotate hourly, granular permissions, higher rate limits

### Core Operations
| Operation | GraphQL | Usage |
|-----------|---------|-------|
| Find project | `organization.projectV2(number:)` | Startup |
| Get field definitions | `node.fields` on ProjectV2 | Startup (cache option IDs) |
| Poll board items | `node.items` on ProjectV2 | Every 30-60s |
| Move card (update status) | `updateProjectV2ItemFieldValue` | Card transitions |
| Read issue details | `node` on Issue (body, comments, timeline) | Card dispatch |
| Read PR reviews | `node` on PullRequest (reviews) | Review monitoring |
| Check mergeability | `PullRequest.mergeable` + `reviewDecision` | Pre-merge check |
| Squash-merge PR | `mergePullRequest(mergeMethod: SQUASH)` | After approval |
| Add comment | `addComment(subjectId:, body:)` | Blocked cards / questions |

### Rate Limiting
- 5,000 points/hour (GraphQL point-based system)
- Lightweight poll query: ~3-5 points per invocation
- At 30-60s intervals: ~180-600 points/hour, well within budget
- Include `rateLimit { cost remaining resetAt }` in every query

### Human vs Bot Review Discrimination
- Query `author { __typename login }` on reviews
- `__typename: "Bot"` for automated reviewers, `"User"` for humans
- **Primary approach (per FR-006)**: Maintain configured human reviewer allowlist; only act on reviews where `author.login` is in the list

---

## R5: Ralph-Style Iterative Development Workflow

**Decision**: The coordinare manages a two-layer autonomous development loop — the coordinare is the outer orchestrator (board lifecycle), while project agents run the inner Ralph-style loop (plan-code-test-fix).

**Rationale**: "Ralph-style workflow" refers to the iterative autonomous agent development loop: Plan → Implement → Validate → Fix → Repeat. This is the dominant pattern in AI-powered coding agents (SWE-agent, Devin, aider, Claude Code). The coordinare maps board columns to loop phases.

**Key Mapping**:
| Board Column | Loop Phase | Activity |
|-------------|-----------|----------|
| ToDo / Backlog | Task Intake | Card waiting to be dispatched |
| In Progress | Plan + Implement + Validate + Fix | Agent running inner loop |
| Blocked | Escalation | Max retries / needs clarification |
| In Review | Human Validation | PR open, awaiting review |
| Done | Completion | PR merged, card closed |

**Error Handling Strategy**:
- Inner loop (agent): Retry code fixes up to N times, then re-plan up to M times, then escalate to Blocked
- Outer loop (coordinare): Retry transient API failures with exponential backoff, never lose card state
- Max iterations: Configurable per project; default 3 fix attempts, 2 re-plan attempts before blocking

---

## R6: Recommended Package Stack

| Package | Purpose | Version |
|---------|---------|---------|
| `langgraph` | State machine orchestration, persistence | >=0.2, pin exact |
| `langgraph-checkpoint-postgres` | Production state persistence | Latest stable |
| `anthropic` | Claude API for reasoning tasks | >=0.40 |
| `gql[aiohttp]` | GitHub Projects GraphQL client | Latest stable |
| `asyncssh` | SSH to agent hosts (FR-019) | Latest stable |
| `structlog` | Structured JSON logging (FR-020) | Latest stable |
| `aiosmtplib` | Async email notifications (FR-009) | Latest stable |
| `slack-sdk` | Slack notifications (FR-010) | Latest stable |
| `fastapi` + `uvicorn` | Health-check endpoint (FR-021) | Latest stable |
| `prometheus-client` | Metrics export (FR-022) | Latest stable |
| `pydantic` + `pydantic-settings` | Config validation, env var layering (FR-015) | v2 |
| `pytest` + `pytest-asyncio` | Testing framework | Latest stable |
| `ruff` | Linting and formatting | Latest stable |
| `mypy` | Static type checking | Latest stable |

**Excluded**: `langchain` (unnecessary), `langchain-anthropic` (use native SDK), `crewai` (wrong paradigm), `PyGithub` (REST-only, no Projects v2 support).
