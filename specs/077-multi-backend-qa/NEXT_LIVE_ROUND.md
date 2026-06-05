# 077 — Next Live Diverse-Backend Round (resume prompt)

## Goal
Run the full card lifecycle end-to-end with each role on its mapped backend, all
driving self-hosted Spark models, and produce the final per-backend findings
report. All known integration bugs are fixed (junie, codex/ephemeral ci_gate,
openclaw, claude_code, hermes, opencode, pi, Thum.io LOCAL_CAPTURE_RULE) and the
freshly rebuilt `coordinare-performer:full` image is verified.

## Settled model tiering (from findings.md)
- **Judgment (assessor / reviewer / qa) → `gpt-oss:120b`** — assessor needs JSON
  format-compliance (20b too weak, qwen prose-fails); reviewer + qa need grounded
  judgment. claude_code(qa) via LiteLLM shim; openclaw(reviewer) Ollama-direct.
- **Builders (architect / implementer / security) → `qwen3.6:35b`** — fast, contract-faithful.
- **Mechanical (closer / env_bootstrap / tech_writer) → `qwen3.6:35b`** — swap-on-failure only.
- llama3.3:70b judgment tier was TRIED and REVERTED (off-task, 102GB VRAM).

## Role → backend mapping (website symphony)
assessor→junie · architect/implementer/security→codex · reviewer→openclaw ·
qa→claude_code · tech_writer→hermes · env_bootstrap→opencode · closer→pi

## Routing gotchas (do NOT regress)
- claude_code is **thinking-gated** — only route to reasoning-capable models.
- gpt-oss harmony tool_calls leak is intermittent → keep openclaw **Ollama-direct**.
- junie/hermes pin model in endpoint env (`JUNIE_PROVIDER_MODEL`/`HERMES_MODEL`),
  not the dispatch payload — must override for a single-model run.
- ephemeral implementer ci_gate must NOT require a still-pending async check
  (scope `persona_check_map` to checks complete by implementer finish).

## Steps
1. `set -a && source .env && set +a` then restart coordinare (use restart-coordinare
   skill; `--rebuild` only if performer code changed — image is already current).
2. Duplicate the time-tracking card (#151, in TODO) into the website board.
3. Watch it move assess→architect→implement→review→security→qa→docs→close, each
   stage on its mapped backend. Confirm via LiteLLM/Ollama traffic that every
   stage drives the intended Spark model (SC-002 shared-model invariant).
4. Capture per-stage outcome + any new backend failure mode into
   `specs/077-multi-backend-qa/findings.md` (Phase-9 format).
5. Update `tasks.md` checkboxes; finalize the findings report; PR #95 is already
   mergeable — land it once the live round is documented.

## Watchpoints
- Model can stall a turn ~1h (no role_timeouts[implementing]) — consider a default
  per-role timeout / upstream-request watchdog.
- qa is low-signal on visual cards (no browser in ephemeral container) — lean on
  feature tests, not the "visual capture" narrative.
- Slow non-converging single-tool churn is invisible to the 15-min stall watchdog.
