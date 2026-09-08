# Plan

Python asyncio, existing RoleWorkflow/Toolkit/WorkflowAdapter. No new dependencies.

- Register workflow on both sides; daemon forwards only the env_bootstrap role's workflow
  options and bounds its timeout by the daemon budget. Add Score artifact-presence flags.
- Add a bootstrap environment primitive behind Toolkit, keeping filesystem and service
  execution outside workflow policy. It snapshots promised files in memory, checks identity
  and contents before/after turns and before verification, and calls existing inference,
  readiness, verification functions. A named step method provides execution access.
- Workflow owns the ordered sequence and bounded repairs using Toolkit.run_agent_turn.
  Budget is a single asyncio timeout; cancellation propagates through Toolkit. Each step
  emits progress and records duration in WorkflowMetrics. Report includes status, reason,
  inference summary, repair count. No direct model call.
- Main recognizes the configured workflow's structured report before the legacy bootstrap
  tail. Preserve old tail and protocol literals; daemon still verifies the consumer cache.
- Fix verification subprocess cleanup if the new outer timeout exposes an orphan process.
- Document configuration, strict opted-in verifier semantics and limits. Contract registry
  records new Score fields; existing workflow/workflow_env fields gain bootstrap producer.

Constitution: deterministic seam tests and mutations, no dependency additions, bounded time
and attempts measured in fixtures. Review follows user-requested ship-issue with recorded
substitution if review skill unavailable. Work is isolated in branch 174-env-bootstrap-workflow.
