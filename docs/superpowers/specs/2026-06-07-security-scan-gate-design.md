# Security Scan Gate — Design (spec 083)

**Status:** Draft for review
**Date:** 2026-06-07
**Branch:** `083-security-scan-gate` (own spec; NOT folded into 082-qa-cycle per PR-scope discipline)

## Problem

The `security` performer role (`src/coordinare/services/persona_service.py:326`) produces a **pure-LLM verdict**: it receives the PR diff, is told to look for OWASP Top 10 / insecure patterns, and emits `{"passed": bool, "findings": [...]}`. There is no deterministic backstop — the verdict is entirely the model's opinion.

The 082 executor sweep proved the failure mode: **all five tool-capable local executors** (qwen3.6:35b, qwq:32b, qwen2.5:14b-instruct, qwen2.5:32b, qwen3-coder:30b) wrongly **passed** a deliberately vulnerable bench PR. Only gpt-oss:120b reliably caught it. Production currently binds `security` to gpt-oss:120b, so prod is presently safe — but the safety rests on a single model on a single harness, with no floor underneath it. One config edit, model regression, or harness quirk silently removes the only protection.

## Goal

Make the `security` verdict **no longer purely model judgment** by adding a deterministic static-analysis floor plus structural and config-level reinforcements. One sentence: *a critical/high static-analysis hit must block the PR regardless of what the model concludes.*

## Scope — three levers, one spec, prioritized

| Lever | Priority | Nature | Adds |
|-------|----------|--------|------|
| Static-analysis hybrid gate (semgrep/bandit) | **P1** | New service + dispatch/verdict integration + image tool | Detection power independent of the model (the real fix) |
| CWE taint→sink checklist | **P2** | Prompt restructure in `persona_service.py` | Raises the ceiling for every judge; model-agnostic |
| Higher-capability judge gate | **P3** | Config load-time validation | Codifies "security must run on a capable model" |

P1 is load-bearing. P2/P3 are small complements. Each is independently testable and shippable. If P1 grows, P2/P3 split to their own spec.

## Architecture — dual-run hybrid (P1)

Coordinare owns the **floor** (deterministic, authoritative). The performer gets the **ceiling** (advisory tool the model reasons with).

### Coordinare leg (authoritative)
1. **At dispatch** (`src/coordinare/graph/nodes/dispatch_performer.py` ~670, when `role == "security"`): coordinare fetches the PR diff (it already holds the GH token), runs semgrep + bandit over the changed files **once**, normalizes findings to the existing finding schema, and **stashes** them in graph state (`state["scanner_findings"]`).
2. **Prompt injection:** the stashed findings are added to `card_context` as a `scanner_findings` block so the security model reasons over real scanner output (the ceiling).
3. **Floor enforcement** (`src/coordinare/graph/nodes/monitor_performer.py` ~2196, where the security verdict is consumed): before accepting `security_passed`, coordinare re-reads the **same stashed findings**. If any are critical/high, it **forces `security_failed`** and merges the scanner findings into `relay_feedback` — *regardless of the model's `passed` value*. The model cannot override the floor.

**Run-once, reuse:** the coordinare scan executes a single time at dispatch; results are reused for both prompt-injection and the verdict floor. No double coordinare run.

### Performer leg (advisory ceiling)
semgrep + bandit are installed in `agent/performer/Dockerfile.full` (the same layer that already carries `ruff`/`black`/`shellcheck`/`eslint` — the `Dockerfile.base` source-COPY invariant is unaffected) and exposed as a performer tool, so the security model can run targeted scans live during review. These feed the model's own `findings[]` but are **not authoritative** on their own — only the coordinare leg binds the verdict.

## Components & data flow (P1)

- **`src/coordinare/services/security_scanner.py` (new).** `scan_diff(changed_files, repo_root) -> list[Finding]`. Wraps semgrep (`--config auto --json`) and bandit (`-f json`), normalizes both into the spec-022 finding schema (`severity`, `category`, `description`, `file`, `line`, `routing`). Pure and deterministic; unit-testable with fixture diffs. Scanner findings default to `routing: implementer` (code-level); the model may re-route in its own findings.
- **Diff acquisition.** Extend the existing GitHub helper with `get_pr_diff(pr_url) -> (raw_diff, changed_files)` (via `gh pr diff` / GitHub API).
- **Dispatch integration** (`dispatch_performer.py`): call scanner when `role == "security"`, stash in `state["scanner_findings"]`, add `scanner_findings` block to `card_context`.
- **Floor enforcement** (`monitor_performer.py`): override `security_passed` → `security_failed` when stashed findings contain critical/high; merge into `relay_feedback`.
- **Protocol:** reuses `ProtocolResponse.findings` (`src/coordinare/protocol.py:61`) and the `security_passed`/`security_failed` status enum — no schema change required.

## CWE taint→sink checklist (P2)

Rewrite the `security` persona string (`persona_service.py:326`) into a structured pass while keeping the JSON output contract unchanged:
1. Enumerate untrusted **sources** introduced/touched in the diff (user input, network, env, file, deserialized data).
2. Trace each source to dangerous **sinks** (exec/eval, SQL, shell, path ops, template render, HTTP egress).
3. Walk a fixed short **CWE list** — injection, broken authz, hardcoded secrets, insecure deserialization, path traversal, SSRF — and for each state: checked / clear / finding.
4. Emit the same `{passed, findings[]}` JSON.

Model-agnostic; raises the ceiling for every judge regardless of capability.

## Higher-capability judge gate (P3)

Add **load-time config validation** in `src/coordinare/config.py`: if the `security` role resolves to a model on a **known-weak denylist** (the five 082 executors: qwen3.6:35b, qwq:32b, qwen2.5:14b-instruct, qwen2.5:32b, qwen3-coder:30b), raise a **validation error** at config load. Scope: **`security` role only** (matches the 082 finding directly; reviewer/assessor not in scope for this spec). Codifies the production lever so a future config edit cannot silently bind security to a weak judge.

## Error handling & edge cases — FAIL-CLOSED

- **Scanner unavailable / crashes / times out, or diff fetch fails:** the gate is **fail-closed**. Coordinare emits `security_failed` carrying a synthetic **critical** finding `category: scanner_unavailable` with a clear description, and a loud observability marker. To avoid a pointless implementer fix-loop (an implementer cannot fix broken tooling), this finding **routes to halt/human attention** rather than back to `implementer`. The floor is never silently absent.
- **Non-Python/JS diffs:** semgrep `--config auto` covers many languages; bandit is Python-only and simply yields nothing for other languages — not an error.
- **Secrets discipline:** the scanner sees only repo diff content (review material, not secrets) and must never echo `auth_env`-resolved values. Diff text and any tokens stay out of INFO logs (FR-019 discipline from 073/080). INFO limited to scan summary (counts/severities), not raw diff.

## Testing (TDD — RED first)

- `tests/unit/services/test_security_scanner.py`: fixture diff with a known injection → normalized critical finding; clean diff → empty; semgrep-only and bandit-only fixtures; malformed scanner output handled.
- `tests/unit/graph/nodes/test_monitor_performer.py`:
  - **Core 082 regression:** scanner critical + model `passed:true` → verdict overridden to `security_failed`.
  - scanner medium/low + model pass → **no** override.
  - scanner error/unavailable → fail-closed `security_failed` with `scanner_unavailable` finding routed to halt.
- `tests/unit/test_persona_service.py`: security prompt contains the CWE taint→sink checklist structure.
- `tests/unit/test_config*.py`: `security` role on a denylisted model → validation error at load; capable model → no error.
- **End-to-end proof:** re-run the 082 vulnerable bench PR through the gated security role → expect `security_failed`. This is the acceptance bar for the whole spec.

## Decomposition & order

P1 → P2 → P3 within one spec. P1 is the load-bearing fix and must land first (it carries the e2e acceptance test). P2 and P3 are small, independent, and can land in any order after P1.

## Non-goals

- No change to reviewer/assessor judge binding (only `security` for P3).
- No new persisted coordinare state (scanner findings live in transient graph state, mirroring `relay_feedback`).
- No replacement of the model verdict — the model still reviews; the scanner adds a floor and feeds context.
