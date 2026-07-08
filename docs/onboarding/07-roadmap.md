# 07 — Roadmap

Coordinare is built **spec-by-spec**: each feature is a numbered `specs/NNN-name/` directory with
`spec.md` (+ usually `plan.md`, `tasks.md`). There are ~129 specs; this page groups them into
the thematic lines so you can see the arc and what's next.

## How development works here

- Every feature gets a **spec** (`speckit.specify` → `plan` → `tasks` → `analyze` → `implement`).
- Work happens on a **branch named after the spec** (e.g. `119-malformed-output-retry`), with
  TDD and **adversarial review before merge**.
- **Only humans approve** PRs; merges are squash to `main`.
- The `specs/` directory is the **source of truth** for any behavior — when a doc says "spec
  NNN," read that spec.

## Thematic spec lines

### Core orchestration & lifecycle (001–057)
Board polling, the LangGraph workflow, performer lifecycle (019), personas (018), multi-card
parallelism, GitHub auth/Enterprise, per-role model selection, cost tracking, dashboard, stale
branch cleanup (052), PR churn guards (053), **containerized performers (056)**, **multi-project
symphonies (057)**. *Foundational — implemented.*

### Gates & autonomy (064, 074–076, 083, 088–090, 095, 118–119)
The control layer that makes autonomous merging safe:
- **074** persona-scope tiering · **075** implementer CI gate · **076** dispatcher dedup/reconcile
- **083** security-scan gate (deterministic floor + weak-judge model denylist)
- **088** bootstrap circuit breaker · **089** local-test gate
- **090** baseline-repair autonomy (prevention / classification / inherited-repair)
- **095** env-blocked classification (infra failures → HOLD, not bounce)
- **118** runner-infra (hostedtoolcache EACCES) → ENV_BLOCKED · **119** malformed-output retry
*Mostly implemented; the most active area.*

### Multi-backend & self-hosted model parity (067, 073, 077, 078, 084, 098–100, 122)
Making off-the-shelf harnesses work against local open models:
- **067** LCD OpenAI-compatible backends · **073** LiteLLM proxy + ClaudeCodeShim
- **077** multi-backend QA round · **078** self-hosted robustness layer (routing + normalizers + health gating)
- **084** Anthropic⇄OpenAI wire translation · **098** control-char normalizer + junie resilience
- **099** health-probe modes · **100** route hermes (tech_writer) through the normalize shim
- **122** LiteLLM consolidation — **all** self-hosted inference now goes through one LiteLLM
  gateway (no more direct Ollama/Spark calls); a single place to route, observe, and swap models
*Active — this is the shim/normalizer line ([doc 04](04-harnesses-and-shims.md)).*

### Pipeline correctness & PR-lifecycle robustness (120, 123, 125–129)
Making the autonomous lifecycle trustworthy end-to-end — no false passes, no cards stuck forever:
- **120** QA evidence integrity (a self-reported `qa_passed` is never trusted blindly)
- **123** pipeline flow optimizations · **125** stage-verdict memory (skip re-verifying unchanged input)
- **126** terminal-success progress floors (verify a performer's "done" carries real progress)
- **127** approval/feedback race (never merge over an unprocessed actionable review)
- **128** stale-review handling (surface/re-request an addressed human `CHANGES_REQUESTED`)
- **129** BLOCKED-card auto-recovery (re-evaluate & auto-unblock when the blocker clears) +
  QA visual-capture env resilience (missing capture tooling → recoverable HOLD, never a false-pass)
*Implemented; 129 is default-OFF behind `COORDINARE_BLOCKED_RECOVERY` pending broader validation.*

### Living documentation (124)
**124** — the `tech_writer` maintains a **living project wiki** (`docs/wiki/`) via its reliable
`{files}` contract on every documenting stage. (The external "OpenWiki" backend was evaluated and
**dropped** — DeepAgents tool-calling was too flaky across the self-hosted models.)

### Dual-model orchestration (080)
Pair a **thinking** model with a **tool** model in one turn (strategies `single` / `always` /
`conditional` / `think_once`); unifies model selection via the `endpoints[]` / `model_endpoints[]`
/ `modes[]` catalogs. *Architecture + catalogs in place; orchestration in progress.*

### Environment cache & services (060, 087, 091–117)
The per-project dev environment ([doc 06](06-env-cache.md)):
- **060** env-caching foundation · **087** deterministic native-lib activation
- **091–115** coordinare-managed stateful services (the deb-fetch/scripts/readiness subsystem)
- **116** performer-owned services (default-OFF toggle — *reverts 091–115 to dormant*)
- **117** services start at activation in the performer's container
*060/087/116/117 implemented; 091–115 dormant behind the 116 toggle.*

## Status snapshot

| Line | State |
|---|---|
| Core orchestration / lifecycle / config | ✅ implemented |
| Gates & autonomy (075/089/090/095/118/119) | ✅ implemented, actively extended |
| Self-hosted parity (078/084/098/099/100) | ✅ implemented, actively extended |
| LiteLLM gateway consolidation (122) | ✅ all self-hosted inference behind one gateway |
| Pipeline correctness & PR-lifecycle (120/123/125–128) | ✅ implemented |
| BLOCKED auto-recovery + QA capture resilience (129) | ✅ implemented, **default-OFF** pending validation |
| Living wiki documenter (124) | ✅ implemented (own `tech_writer`; OpenWiki dropped) |
| Dual-model orchestration (080) | 🔶 catalogs + resolution in place; orchestration in progress |
| Env-cache (060/087/116/117) | ✅ implemented; performer-owned by default |
| Coordinare-managed services (091–115) | 💤 dormant (kept behind the 116 toggle) |

## Likely next steps

1. **Finish dual-model (080):** complete the planner/executor proxy + catalog-based dispatch so
   diverse backends can pair a thinking model with a tool model per turn.
2. **Harden self-hosted parity:** more normalizers / health-probe coverage as new local-model
   pathologies surface (the 078→100 line is demand-driven by real failures).
3. **Security floor (083):** complete the deterministic static-analysis floor + enforce the
   weak-judge denylist everywhere.
4. **Observability:** surface routing-table status, bootstrap circuit-breaker state, and
   dual-model selection in the dashboard.
5. **Reduce local-model fragility:** the 098/119 retry+capture pattern (and `performer_log_dir`
   forensics) is the template — extend resilience to remaining contract-bound roles as needed.

> The throughline of the recent roadmap: **make a team of cheaper, self-hosted models reliably do
> frontier-grade engineering work** — via gates that contain their failures, shims that fix their
> output, and an env-cache that gives them a real place to build and test.

Back to **[README](README.md)**.
</content>
