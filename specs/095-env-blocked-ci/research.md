# Phase 0 Research: ENV_BLOCKED CI-Failure Classification

## R1. Where ENV_BLOCKED slots into the classification order

**Decision**: Evaluate the env-signature match **first** — before the existing rows in `classify_failure_origin` (services/failure_classification.py:112). If the head failure's normalized reason matches an infra pattern, return `env_blocked`; otherwise fall through to today's logic (flake → unknown → introduced → inherited).

**Findings**:
- `classify_failure_origin` is a pure, row-ordered function: Row 1 transient HEAD → FLAKE; Row 2 indeterminate base → UNKNOWN; Row 5 base-no-failure → INTRODUCED; Row 4 stable+match → INHERITED; else INTRODUCED.
- The #159 failure is a *stable* "failure" conclusion (artifact upload step) that ALSO fails on the baseline → today it hits Row 4 → INHERITED → repair-eligible. ENV_BLOCKED must pre-empt that.
- An env failure can present as stable on HEAD; it must NOT first be siphoned to FLAKE/INHERITED. So the env check is a new **Row 0**.

**Rationale**: ENV_BLOCKED is never a repair candidate and never the card's fault, so it must short-circuit the whole inherited/introduced/flake decision (spec FR-001, "evaluated before repair eligibility").

**Alternatives considered**: Detect env failures *after* classification and override — rejected: it would let the inherited/repair machinery briefly treat it as repairable, and complicates the L3 mandate gating. A clean Row-0 short-circuit is simpler and safer.

## R2. Signature matching — pure function, keyed on the existing normalized reason

**Decision**: New `services/env_signature.py` with a pure `match_env_signature(reason: str, patterns) -> EnvCause | None`. Reuse `failure_signature.normalize_reason(title, summary)` to derive the reason, then match it against an ordered list of patterns. Each pattern carries `{id, regex, cause, action}`; first match wins; no match → `None` (fail-safe, FR-003).

**Findings**:
- `normalize_reason` already lowercases, collapses whitespace, and strips volatile drift (hashes/uuids/timestamps) — ideal for stable substring/regex matching of messages like "artifact storage quota has been hit" / "createartifact … quota".
- Built-in patterns to ship (FR-002): artifact-storage quota (`createartifact|artifact storage quota|storage quota has been hit`), runner offline/unavailable (`no runner|runner.*offline|waiting for a runner|this check was cancelled because.*runner`), billing/spending-limit (`spending limit|billing|payment`).

**Rationale**: A pure matcher is trivially unit-testable (SC-005 anti-false-positive), and keying on the already-computed normalized reason avoids new parsing.

**Alternatives considered**: Match raw titles/summaries — rejected (volatile drift causes brittle matches; normalize_reason exists for exactly this).

## R3. Config — EnvBlockedGateConfig under persona_scope (mirror the 090 gates)

**Decision**: Add `EnvBlockedGateConfig` (pydantic, `extra="forbid"`) on `PersonaScopeConfig` alongside the 090 gates (config.py ~1429): `enabled: bool = False` plus `patterns: list[EnvSignaturePattern] = []` (operator additions) with built-in defaults applied in the matcher when enabled. Resolved per-symphony exactly like `_get_baseline_classification_gate_config` (monitor_performer.py:981).

**Findings**: The 090 gates live on `persona_scope` and are resolved per-symphony, independent of `persona_scope.enabled`. ENV_BLOCKED is the same shape → operators enable it per symphony; default-off keeps the baseline (FR-012, SC-006).

**Rationale**: Consistency with the established 090 gate pattern; operator-extensible patterns satisfy FR-002 without code changes.

**Alternatives considered**: A global (non-per-symphony) config — rejected; per-symphony matches how 090/074 gates are scoped and lets different repos carry different infra patterns.

## R4. Per-card state — dedup + auto-clear

**Decision**: Add a per-card `env_blocked` field to the persisted session (state_store.py PersistedSession), e.g. `env_blocked: {signature_id, notified_at} | None`. Set when classification returns env_blocked; used to dedup the operator notification (FR-006); cleared when a subsequent evaluation yields no env match (FR-008, auto-resume). Schema-version bump; old snapshots default to `None`.

**Findings**: 090 already persists per-card repair counters on PersistedSession with backward-compatible defaults and schema bumps — the same pattern applies. Per-card (not global) so one card's infra block doesn't suppress another's notification.

**Rationale**: Minimal durable state for once-per-condition notification and self-clearing hold across daemon restarts.

**Alternatives considered**: In-memory only — rejected; a restart would re-notify (violates FR-006 dedup across restarts), the same lesson as 069's blocked-notification watermark.

## R5. Hold / surface wiring + notification

**Decision**: At the classification/repair decision point in `monitor_performer.py`: when any required failing check is `env_blocked`, (a) do NOT build the L3 repair mandate and do NOT re-dispatch/bounce (hold), (b) emit a distinct operator notification via `notify.py` naming cause + action, deduped on the per-card `env_blocked.notified_at`. A non-env failure on another check still classifies normally (FR-009). On a later evaluation with no env match, clear the hold and resume (FR-008).

**Findings**: `monitor_performer.py` already builds the 090-L3 `repair_mandate` and gates dispatch; ENV_BLOCKED is a guard inserted ahead of that. `notify.py` already has deduped operator messaging (069 watermark pattern) to mirror.

**Rationale**: Reuses existing decision/notify points; the env guard is additive and short-circuiting.

**Alternatives considered**: A separate board column / GitHub label for env-blocked — out of scope; the spec asks for operator notification + hold, not a new board state.

## R6. Anti-masking & fail-safe (cross-cutting)

**Decision**: Env detection runs on the HEAD failure alone (no baseline dependency, FR-011). A reason that doesn't match any pattern is never env_blocked (FR-003). A mixed card (env-blocked check + introduced check) holds on the env block but still classifies+surfaces the introduced failure (FR-009). Non-required checks never trigger a hold (mirror 090 L1 required-set policy).

**Open item carried to implementation**: R1's exact Row-0 placement is locked by a test — the #159 stable-failure-that-also-fails-on-baseline case must come out `env_blocked`, not `inherited`/`flake`. That reproduction test is written first (TDD).
