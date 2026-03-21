# Research: Performer Lifecycle

**Branch**: `019-performer-lifecycle` | **Date**: 2026-03-18

## Decision 1: Registry Pattern vs Per-Role Node Pairs

**Decision**: Single `dispatch_performer` + `monitor_performer` node pair resolving the active service from a `performer_services: dict[str, AgentServiceProtocol]` registry.

**Rationale**:
- Adding a new performer role requires only a new config entry — zero graph code changes
- The graph does not branch on role identity; role selection is a data lookup
- Consistent with the performer wire protocol (all roles speak the same JSON stdin/stdout protocol)
- `performer_stage` in `CoordinareState` is the single source of truth for which role is active

**Alternatives considered**:
- Per-role node pairs (dispatch_architect / monitor_architect, etc.) — N×2 nodes for N roles; adding a role requires graph code changes; violates open/closed principle
- Single node with embedded if/elif chain — equivalent to per-role pairs but in one file; same maintainability problem

---

## Decision 2: Sequential vs Parallel Role Execution

**Decision**: Strictly sequential — each role completes before the next begins.

**Rationale**: User-confirmed (2026-03-18): sequential execution reduces operational complexity of the feedback cycle. Parallelisation is a future optimisation if needed.

**Alternatives considered**:
- Parallel execution of independent roles — would require cross-role coordination, merge strategies for conflicting changes, and significantly more complex state management

---

## Decision 3: Lifecycle Sequence Derivation

**Decision**: The `lifecycle_sequence` is derived at startup from the order in which roles appear in `config.yaml`, filtered by which roles are non-None in `PerformersConfig`. The canonical order is: `advocate → assessing → architecting → implementing → reviewing → security → qa → documenting`.

**Rationale**:
- Config-driven: operators skip roles by omitting them from config; no code change required
- Canonical order is enforced in code (not config) to prevent operators from accidentally running QA before implementation
- Sequence is computed once at startup and stored in `CoordinareState`; nodes do not recompute it

**Alternatives considered**:
- User-configurable sequence order — dangerous (QA before implementation, etc.); adds validation complexity with no clear benefit
- Hardcoded full sequence every time — would require code change to skip a role; less flexible

---

## Decision 4: Backward Compatibility Strategy

**Decision**: If `performers` is absent from `config.yaml` (existing deployments), the bootstrap falls back to implementer-only mode using the existing `agent_transport` config. `lifecycle_sequence = ["implementing"]` and `performer_stage = "implementing"`. Behavior is identical to pre-019.

**Rationale**:
- Zero-migration path for existing operators
- No breaking changes to existing config files
- The fallback is explicit code, not implicit; it can be removed in a future version once all operators migrate

**Alternatives considered**:
- Require new `performers:` key from day one — breaks existing deployments; migration cost disproportionate to benefit
- Auto-migrate old config at startup — adds complexity and is fragile (YAML round-tripping loses comments)

---

## Decision 5: New Phase Value

**Decision**: Rename the in-flight monitoring phase from `"monitoring_agent"` to `"monitoring_performer"` in `CoordinareState.phase`.

**Rationale**:
- `"monitoring_agent"` implies a single agent; the new lifecycle supports many sequential performers
- The graph routing conditions already use phase strings as routing keys; updating to `"monitoring_performer"` is a clean rename with no semantic change

**Alternatives considered**:
- Keep `"monitoring_agent"` — misleading naming that would confuse future contributors; minor saving with high long-term confusion cost

---

## Decision 6: `classify_human_feedback` Classification Strategy

**Decision (V1)**: Use keyword heuristics to classify PR comments into concern categories (implementation, architecture, security, documentation, qa, review). This provides fast, deterministic routing without requiring an AI service call on every PR comment.

**V2 (future)**: Upgrade to AI-powered classification using the existing Claude service for ambiguous comments that keyword matching cannot confidently classify.

**Rationale for V1**:
- Keyword matching is fast, deterministic, and requires no external API call
- Most PR comments contain clear concern signals ("bug", "security", "test coverage") that keywords reliably detect
- Reduces operational cost and latency compared to AI classification on every comment
- Falls back to "implementing" when no keywords match — safe default

**Alternatives considered**:
- AI-only classification from day one — adds API latency and cost for every PR comment, overkill for V1
- Separate AI service for classification — over-engineered for the current scale

---

## Decision 7: Terminal Success State Set

**Decision**: The `monitor_performer` node recognises these terminal success states and triggers lifecycle advancement:

| State | Emitted by |
|-------|-----------|
| `pr_opened` | implementer (existing) |
| `plan_committed` | architect (020) |
| `approved` | reviewer (021) |
| `security_passed` | security (022) |
| `qa_passed` | QA (023) |
| `docs_committed` | tech writer (024) |

**Rationale**: Each role reports a role-specific terminal state rather than a generic `success`. This makes logs and state transitions self-documenting and prevents accidental cross-role state conflation.

**Alternatives considered**:
- Generic `done` state for all roles — simpler but loses role-specific information; harder to debug
