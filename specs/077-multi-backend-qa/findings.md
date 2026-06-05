# Per-Backend Findings — 077 Diverse Multi-Backend QA Round

Captured during the live diverse-backend run (US5 / FR-010 / SC-007). Mirrors
spec 076's "Phase 9 live-test fixes" format.

**Stage-pass rule:** a backend is `contract-respecting` for a role when it ran,
drove `spark/qwen3.6:35b` via LiteLLM, and returned a valid terminal contract
(DONE / PARTIAL_PROGRESS / BLOCKED) — regardless of whether the card merged.
Model-quality issues are findings, not backend failures.

## Backend × role verdicts

| Backend | Role(s) | Verdict | Evidence | Follow-up |
|---|---|---|---|---|
| junie | assessor | **contract-respecting (after 4 fixes)** | Drove `spark/qwen3.6:35b` (6 calls) after EROFS + unzip + id-field + full-endpoint-baseURL fixes; assessor stage `terminal_state=succeeded` | 4 fixes committed (1264f9b, a460896, 97919e2) |
| codex | architect, implementer | **contract-respecting; model-quality ceiling** | architect succeeded; implementer opened PR #152 (CI green on re-run) but over-explores on qwen (~5–8M tokens, ~200 tool calls); introduced a duplicate-form regression on the first attempt (CI-caught) | — |
| claude_code | qa | _pending (downstream of review loop)_ | | |
| openclaw | reviewer | **contract-respecting; needed a workspace fix** | `performer_id=openclaw-ephemeral` ran the reviewing stage; posted `CHANGES REQUESTED` reviews on PR #152 (valid binary verdict), drove the shared model. **Root-caused** the "workspace essentially empty" reviews: openclaw's `--local` agent operates only in `$HOME/.openclaw/workspace` and **ignores the process cwd**, so it never saw the PR checkout → always requested changes → review↔implement loop never converged. | **Fixed:** `OpenClawBackend._point_workspace_at` symlinks `~/.openclaw/workspace` → `stand.path` so the agent sees the real repo + diff (2 unit tests). Also a transient `http_performer.secret_refresh_failed` on teardown (non-fatal). |
| hermes | tech_writer | _pending (downstream)_ | | |
| opencode | env_bootstrap | _pending (warm cache; no cold bootstrap yet)_ | | |
| pi | closer | _pending (downstream)_ | | |
| **CI gate (075)** | implementer | **works; scoped per FR-013** | caught codex's duplicate-form regression (bounced/blocked); after FR-013 decoupling the implementer gate is scoped via `persona_check_map.any` — validated live: `resolver_source=persona_check_map, verdict=pass`, advancing #93 past the implementer to openclaw | FR-013 landed (c6b9508, ed3c8d4) |

> **Round mapping (7 distinct backends):** assessor→junie, architect/implementer/security→codex, reviewer→openclaw, qa→claude_code, tech_writer→hermes, env_bootstrap→opencode, closer→pi.

## Notes / cross-cutting observations

- **junie (assessor) surfaced 4 real integration bugs** before it could run — all fixed: (1) EROFS writing the custom-LLM profile into the read-only `~/.junie` mount → writable job-scoped `JUNIE_HOME` fallback; (2) `unzip` missing from the base image → junie installer aborted ("No version found") → added `unzip`; (3) profile `id` field must be the **wire model** (`spark/qwen3.6:35b`), not the local profile id → LiteLLM 400; (4) `JUNIE_PROVIDER_BASE_URL` must be the **full endpoint** (`…/v1/chat/completions`), not a `/v1` base (junie POSTs verbatim). After all four, junie drove `spark/qwen3.6:35b` end-to-end (6 calls). Verdict: **contract-respecting after fixes.**
- **codex (architect)** → succeeded. **codex (implementer)** → produced a plausible PR (#152: CSS + form-partial refactor + version bump + card docs) but over-explored on qwen (~200 tool calls, ~7.8M cumulative tokens) and introduced a **duplicate-form regression** (rendered the contact form twice) that broke 2 `:js` feature tests. Verdict: contract-respecting backend; **model-quality + regression ceiling on qwen**.
- **CI gate (075) worked exactly as designed** — it caught the real feature-test regression and bounced/blocked rather than advancing red CI. **But** requiring the *full* PR CI (incl. heavy `:js` feature tests) is too strict for a weak-model implementer, which can't self-fix to green. **Fix landed in-round (FR-013/T040):** decoupled `persona_check_map` from 074 tiering via a depth-agnostic `any` list, so the implementer gate can be scoped (e.g. version/rubocop/unit, excluding feature tests) and deeper checks deferred to reviewer/qa. *Coupling observation:* per-persona gate scoping previously required enabling the 074 persona classifier — now independent.

- **JSON-only contract roles need a *format-literal* model, not the biggest one** (assessor finding). On `spark/qwen3.6:35b` the assessor (junie) prose-failed every retry → `BACKEND_FORMAT_ERROR`. Switching the assessor to `studio/openai/gpt-oss-120b` *also* failed — the 120B "over-helped" with a polished markdown writeup (`### Summary…`) instead of JSON. Switching to the **smaller `studio/openai/gpt-oss-20b` @ 131k context cleared the gate**: 1 format error then valid JSON (`terminal_state=succeeded`), and #93 advanced **assess → architect → implement**. The 20B isn't *reliably* JSON either (it emitted markdown on a failed attempt) but its higher format-compliance rate lands valid JSON **within the 2-retry budget**, where qwen/120B exhausted it. **Conclusion:** for strict JSON-only roles, instruction-literalness beats raw capability — reframes the round's takeaway from "qwen can't do JSON roles" to "JSON roles need a format-compliant model, which isn't necessarily the largest." (lms: both gpt-oss-120b @ 32k/63GB and gpt-oss-20b @ 131k/12GB fit the 128GB box.)

- **codex-ephemeral (implementer) — 075 ci_gate HOLD is incompatible with ephemeral performers → false transport-error block.** *(Root-caused from the full log; supersedes the earlier "job-retention" guess.)* The implementer codex job actually **SUCCEEDED** (19:18:18, `performer_endpoint.transition from_state=busy error_reason=None`, slot released, workspace removed). The 075 implementer `ci_gate` then evaluated and returned **`verdict=hold`** because one required check (`Install dependencies`) was still **pending** on PR #152. The HOLD path (`monitor_performer._evaluate_ci_gate`, ~line 1179) returns `phase="monitoring_performer"` and re-enters the monitor **expecting to re-poll the performer next cycle** — which is fine for a *persistent* performer but **impossible for an ephemeral one**: the one-shot codex container already tore down on terminal success (`_active_jobs` cleared). So the next cycle: (a) check_board `_is_stale` (built for the *daemon-restart* orphan case) sees `has_live_session=False` and **misclassifies the completed session as restart-orphaned** → `stale_session_reconciled decision=fresh_dispatched`; and (b) `monitor_performer` polls the dead session → `ephemeral_job_lookup_miss known_job_ids=[]` → `transport_error "no endpoint resolved yet"` counted toward the system-error retry budget. Repeats every retry → `max_retries_exceeded (3)` → **#93 → Blocked**. Unlike its sibling verdicts, the **HOLD return forgot to clear `agent_dispatch`** (BOUNCE ~886 and ESCALATE ~1239 both clear it with the comment *"Reset dispatch so resume doesn't inherit a stale performer session reference"*). **Root cause:** the 075 implementer ci_gate HOLD-and-re-poll loop assumes a live, re-pollable performer; ephemeral performers tear down on terminal completion, so any HOLD on a *pending* (async) check is fatal. **This is exactly the class of per-backend failure mode 077 set out to surface.** Fix spans 3 heavily-tested sites (ci_gate HOLD return, a `monitor_performer` "ephemeral-implementer-awaiting-CI → re-evaluate gate without re-polling" branch, and check_board `_is_stale` + the multi-session `agent_dispatch` state/per-session desync) — a real design change, not a hot-patch. **FIXED + LIVE-VALIDATED** (commits `c80257d`, `7311e21`, `047a84c`; 4 regression tests; 3239 unit tests green). `monitor_performer` now never polls a torn-down ephemeral implementer session: guarded by `stage==implementing AND phase==monitoring_performer AND _implementer_session_gone`, it either re-evaluates the CI gate directly against GitHub (open PR) or re-dispatches cleanly (no PR) — never cascading into a false transport error. The discriminator deliberately keys on **live** signals (`has_live_session` + open PR), NOT on `latest_ci_gate_decision.verdict` — the live re-validation proved that field does **not** survive the multi-session state round-trip (it persisted as `null`), which had re-blocked the card on the first fix attempt. **Live confirmation:** after the fix, #93 ran assess→architect→implement and entered a clean 075 ci_gate hold→bounce loop (codex re-implementing to get `Validate version` / `Scan for vulnerabilities` / `Unit tests` green) with **zero `ephemeral_job_lookup_miss` across a full hour** — the fatal block became a correct, patient CI-fix loop. Remaining convergence is a codex/qwen model-quality question (can a weak model fix its own CI), not the infra bug. **Immediate mitigation (zero-code, via FR-013):** scope the implementer `persona_check_map` to only checks that are *complete* by the time the (ephemeral) implementer finishes (e.g. `Validate version`, `Verify code quality`) and **exclude slow/async checks** (`Install dependencies`, unit/feature tests) — defer those to the reviewer/qa and the final `monitoring_pr` (064) gate, which polls GitHub directly with no performer. Then the implementer gate **PASSES** instead of HOLDing → advances to the reviewer → never hits the ephemeral re-poll trap. *(Refines the FR-013 finding: an implementer gate must not require a still-pending async check, or it strands an ephemeral implementer.)* Verdict for codex implementer: **the backend ran and succeeded; blocked by a coordinare ci_gate/ephemeral interaction, not by the model or the codex backend.**

- **Model hang with no auto-recovery (spark/qwen + missing role timeout).** During the post-fix live run, a codex implementer turn **hung for ~62 minutes** waiting on the shared model: the codex `app-server` logged `turn started` then went silent (CPU `0:46` over 62 min = blocked on I/O), the job-runner kept reporting "working", and coordinare polled it indefinitely. There is **no `role_timeouts[implementing]` configured**, so nothing killed the stalled turn — the lifecycle simply stalled. It **self-recovered** when the `spark/qwen3.6:35b` call eventually returned (the DGX Spark model was slow/stuck, not dead). **Two takeaways:** (1) the shared long-running self-hosted model can stall a turn for an hour; (2) coordinare should ship a default per-role timeout (or an upstream-request watchdog) so a hung model call is auto-killed + retried rather than stalling the board. Not caused by any backend — a model-latency + missing-timeout gap.

- **openclaw (reviewer) — cannot read the card-documentation files → never converges.** After the ephemeral fix unblocked the implementer→reviewer transition, the openclaw reviewer (`performer_id=openclaw-ephemeral`, `gpt-oss-120b`) **ran and posted a real verdict** — but always `CHANGES REQUESTED` with body: *"Cannot perform review as the card documentation files are not available to read."* This is distinct from (and downstream of) the earlier "empty workspace" symlink fix: openclaw's `--local` agent now sees the repo checkout, but **not the card-documentation context** its reviewer persona is instructed to read against. Result: a **review↔implement bounce loop that cannot converge** — each round the reviewer requests changes, coordinare bounces cleanly back to the implementer (the loop machinery works), codex re-implements (~20–60 min/turn on qwen), and the reviewer again can't read the card docs. **Fix needed:** surface the card description / acceptance-criteria / spec docs into openclaw's managed workspace (alongside the repo symlink) so the reviewer persona can actually read them. Verdict for openclaw reviewer: backend runs + posts valid binary verdicts, but the round's review loop won't converge until card-doc context is mounted into its `--local` workspace.

- **openclaw card-docs fix VALIDATED — review converged, round reached qa (6 backends deep).** After writing the card context to `CARD.md` in openclaw's `--local` workspace (commit `a5e8fee`), the openclaw reviewer stopped posting "Cannot perform review" and gave a real verdict — the **review↔implement loop converged** and #93 advanced **review (openclaw/gpt-oss-120b) → security (codex) → qa (claude_code)**, exercising six of the seven backends in one lifecycle. Confirms both the ephemeral fix and the openclaw card-docs fix end-to-end.

- **claude_code (qa) — crashed on an unsupported CLI flag (`--max-tokens`).** At the qa stage the claude_code backend aborted: `claude exited with code 1: error: unknown option '--max-tokens'`. The Claude Code CLI has **no `--max-tokens` flag**; the 055 output-token cap was being passed as one, so any role configuring `max_tokens` (the qa role did) crashed every claude_code turn. **Fixed** (commit `73128f4`): deliver the cap via the `CLAUDE_CODE_MAX_OUTPUT_TOKENS` env var (the mechanism the CLI honours) instead of the flag; 2 regression tests. Verdict for claude_code qa: backend integration bug, model-independent — fixed.

- **hermes (tech_writer) — two issues found via a pre-emptive one-off, both fixed.** Before #93 reached the tech_writer stage, a controlled one-off (run the `hermes` CLI in the performer image with a prepared `CARD.md` + profile) surfaced: **(1) config incompatibility — BLOCKER.** The entrypoint installs hermes-agent via unpinned `pip install --upgrade` → **0.15.2**, which **ignores** the backend's `providers: <name>: {base_url, key_env, api_mode}` schema → *"no API keys or providers found"* — the tech_writer stage could not even connect to the model. 0.15.2 reads OpenAI-compatible endpoints from a `model:` block instead. **Fixed** (commit `77fd6b6`): `_write_profile_config` now emits `model: {provider: custom, base_url, default: <model>, api_key_env: HERMES_API_KEY}` (key via env, never on disk) and the CLI passes `--provider custom`. Verified: hermes connects with `HERMES_API_KEY` only and drives the model. **(2) card-docs — same family as openclaw.** Hermes runs in `cwd=stand.path` (so no symlink needed, unlike openclaw's `--local`), but a persona that looks for "card documentation files" benefits from `CARD.md`. **Fixed** (commit `ceb1990`): write `CARD.md` + add it to `.git/info/exclude` (hermes COMMITS, unlike the read-only reviewer). **One-off verified hermes reads `CARD.md` verbatim** (title + acceptance criteria). Verdict: backend works after the 0.15.2 config fix; card-docs mount confirmed. *Lesson: pin agent-CLI versions or the entrypoint's `--upgrade` silently breaks a backend on a new release.*

## Shared-model invariant check (SC-002)

- _(record: for each stage, captured LiteLLM traffic confirms `spark/qwen3.6:35b`; flag any vendor-hosted fallback)_

## Shared-model invariant check (SC-002)

- _(record: for each stage, captured LiteLLM traffic confirms `spark/qwen3.6:35b`; flag any vendor-hosted fallback)_

- **openclaw reviewer — ROOT CAUSE: LiteLLM's streaming harmony→`tool_calls` transform for gpt-oss is broken (FIXED by routing openclaw direct to Ollama).** *(Corrects an earlier mis-diagnosis in this doc that blamed openclaw for "not forwarding tools" — it does.)* Every openclaw failure this round (card-docs "files not available", empty `CHANGES REQUESTED`, garbage, `subprocess_exit:1`) traces to one defect, isolated by progressively narrowing the path:
  - openclaw IS correct — a captured outbound request shows a full **22-tool `tools` array** + `tool_choice:auto` + the `read` tool + a correct system prompt, with `stream:true`.
  - The model + LM Studio + LiteLLM-on-replay are correct — replaying openclaw's *exact* request to LiteLLM returns clean structured `tool_calls` (`read({"path":"CARD.md"})`), both streamed and non-streamed.
  - But openclaw's *live* calls through LiteLLM consistently get **raw harmony text** (`<|channel|>commentary to=read <|constrain|>json{...}`) as assistant content instead of `tool_calls`, so the review tools never execute.
  - **Confirmed upstream-known**: LiteLLM's streaming harmony handling for gpt-oss is buggy — openclaw PR #11210 ("Harden Tool-Call Streaming Parser … gpt-oss-120b + LiteLLM"), LiteLLM #17246 (streaming fails to emit `tool_calls`), LiteLLM #13300 (no harmony reasoning support). It intermittently leaks harmony deltas / assembles tool args as `{}`.
  - **FIX (validated end-to-end):** point openclaw's provider at the **Spark's Ollama directly** (`http://192.168.3.30:11434/v1`, model `gpt-oss:120b`), bypassing LiteLLM. Ollama's native gpt-oss harmony→`tool_calls` parsing returns clean `tool_calls` even streamed; in the one-off openclaw **executed the file-read tool and returned the real card content** (no harmony leak). Config: `openclaw-ephemeral` `OPENCLAW_PROVIDER_BASE_URL → 192.168.3.30:11434/v1`, reviewer `model: gpt-oss:120b`.
  - Verdict: openclaw is a **contract-respecting reviewer** when not routed through LiteLLM's broken gpt-oss streaming path. Broader lesson: prefer Ollama-direct (or a translation shim) for gpt-oss + tool-using agents until LiteLLM's harmony streaming support lands (#13300).

  - **UPDATE (LiteLLM-managing agent, 2026-05-31): LiteLLM deployment is NOT the culprit — correction.** A dedicated investigation against the live proxy ran **29 streaming+tools requests** on the implicated path (`studio/openai/gpt-oss-120b` via LM Studio) — **all clean structured `tool_calls`, zero `<|channel|>` leaks** — plus spark/Ollama-direct clean. The harmony→empty-tool_calls streaming bug (#28549, the concrete fix in the #13300/#17246/openclaw#11210 family) is **already deployed** (`litellm v1.87.0-rc.1`, pinned 2026-05-25). My own direct replays of openclaw's exact request were also clean — consistent with this: **LiteLLM is fine.** The harmony text openclaw *received* is therefore **client-side** — openclaw's own streaming tool-call parser (openclaw #11210), most likely mishandling the LM-Studio response shape (`reasoning_content` carrying the harmony CoT alongside `tool_calls`), which the Ollama-direct response shape doesn't trigger. **Net:** Ollama-direct is the *correct* fix (sidesteps openclaw's parser bug end-to-end), not merely a LiteLLM dodge; no LiteLLM change needed (bump to `v1.87.0` only when stable ships). Open confirmation: capture openclaw's actual response bytes from the LiteLLM path (a logging forward-proxy) to prove openclaw receives clean `tool_calls` yet still emits harmony text.

  - **FORWARD-PROXY CAPTURE (2026-05-31): openclaw WORKS via LiteLLM — the leak is intermittent, not deterministic.** Ran openclaw through a logging forward-proxy on the *original failing path* (`studio/openai/gpt-oss-120b` via LiteLLM, NOT Ollama-direct). Result: **openclaw executed its file-read tools and produced a real review verdict** ("Request Changes — styles.css:1 …"), EXIT 0. The 3 captured upstream responses (agentic loop) were **10 / 8 / 0 tool_calls, ZERO `<|channel|>` harmony** — all clean. So openclaw is **not** fundamentally broken on LiteLLM; the earlier failures were the **intermittent** harmony leak the infra agent flagged, which did not reproduce here, in their 29/29, or in direct replays. Root remains un-pinned but **rare/conditional** (likely needs live conditions: real PR checkout, larger context, or model load) — not a deterministic LiteLLM or openclaw defect. **Decision:** keep openclaw on **Ollama-direct** as the reliable route — it avoids the rare leak entirely AND drops the LiteLLM hop — while the infra agent watches spend logs for the harmony pattern to catch a failing `chatcmpl` in the act. Clean baseline chatcmpl ids: q3mvobdwp5bp7fx8otiqw / qc8n5v681bl2ll4rlmgtkj / deebvyu65bfgi3q5a3i3z.

## qa (claude_code) — qwen3.6:35b cannot converge the qa contract → gpt-oss-20b via shim (2026-05-31)

- On card #93 (margins fix, PR #152), qa on `spark/qwen3.6:35b` ran **129 consecutive `tool_use: Bash` turns over ~2h with ZERO Read/Write/edit events and no terminal verdict** (DONE/PARTIAL/BLOCKED) — qwen just churns shell commands without converging. Container log shows each claude_code-shim `/v1/messages` round-trip took **57–73 s** (model latency dominates; 129×~60 s ≈ the 2h).
- This does **not** trip the 077 stall watchdog: every ~60–70 s a *new* Bash event lands, so the progress fingerprint keeps changing — the watchdog only fires on 15 min of a *stable* fingerprint. So a slow non-converging churn is invisible to it. (Possible watchdog enhancement: detect monotone single-tool loops with no file mutation / no terminal verdict over N turns.)
- **Fix (policy: failing persona stage → gpt-oss-20b):** bumped qa `model` → `spark/gpt-oss:20b`. Unlike assessor/reviewer (Ollama-direct), qa **stays on the LiteLLM shim** — claude_code speaks the Anthropic API and Ollama doesn't, so it needs LiteLLM for Anthropic↔OpenAI translation. Route: claude → ClaudeCodeShim → LiteLLM `spark/*` wildcard → the Spark's Ollama gpt-oss:20b (clean serving). The 073 shim strips gpt-oss `reasoning_content`, mitigating the harmony issue for content; remaining risk is only LiteLLM's harmony→tool_calls transform (the intermittent openclaw leak).

## STATE PERSISTENCE GAP — restart rewinds the active card (2026-05-31)

- **Symptom:** swapping qa's model required a daemon restart. After restart, card #93 (which had progressed assess→architect→implement→review→security→qa in memory over ~2.5h) **rewound all the way to `assessing`** and re-dispatched the assessor with the *old* `BACKEND_FORMAT_ERROR` reason still attached.
- **Root cause:** `coordinare.state.json` (default `state_file_path`) was written **once at 20:38 UTC** (6 min after daemon start, capturing the pre-reroute assessor failure) and **never flushed again** for the entire 2.5h run — confirmed by file mtime + the snapshot's `performer_stage: "assessing"` + stale `system_error_reason`. SIGTERM shutdown did not flush either. So all real progress lived only in memory; restart loaded the stale snapshot.
- **Impact on the swap-on-failure workflow:** every mid-card model swap that needs a restart **costs the card's progress** — it restarts the lifecycle from the first-snapshot stage. Mitigation for this round: the re-run is now pre-configured with all three problem stages fixed (assessor + qa → gpt-oss:20b, reviewer → gpt-oss:120b), so it should complete cleanly on the second pass. GitHub side-state (branch + PR #152) survives, so downstream stages can reuse prior work.
- **Action item (likely a fix spec):** persist the state snapshot on every stage transition (and flush on SIGTERM), or reconstruct stage from GitHub/PR state on resume, so restarts don't rewind in-flight cards.

## Implementer is solid; the JUDGMENT stages are the weak link (2026-05-31)

- **Implementer (codex/qwen3.6:35b) produced a correct, on-target diff** for card #93: PR #152 = +211/−56 across 11 files, 6 commits, with exactly-right mobile CSS (`.col-contact` gutter removal, `.contact-panel` edge-to-edge / no card chrome at `max-width:767.98px`, `.contact-accent` bar) **plus a feature spec** (`spec/features/contact/mobile_breakpoint_spec.rb`). So the limited model implements this class of card well.
- **The judgment stages (qa, reviewer) are unreliable on the OSS models.** Multiple qa/reviewer verdicts claimed *"no code modifications are present"* while the 211-line diff was right there; verdicts flip-flop between APPROVED ("margins already reduced") and CHANGES REQUESTED ("nothing to assess") across consecutive runs. This appears on **both** gpt-oss:20b (qa) **and** gpt-oss:120b (reviewer) — i.e. not purely a 20b-capacity issue.
- **qa-specific structural gap:** the qa persona's evidence template demands **visual/browser capture** (open in Chrome DevTools, toggle mobile viewport, screenshot) that the ephemeral performer container can't perform (no headless browser / running server). So LLM-qa is **blind to the rendered result on visual cards regardless of model**, and defaults to `FAILED`. The real QA signal on such cards is the feature test, not the "visual capture" narrative.
- **Decisions:** (a) qa is a reasoning role, not a JSON-format task → staged qa `model` to `spark/gpt-oss:120b` (reviewer tier); applied on next restart to avoid re-rewinding card #93. (b) Note LLM-qa as low-signal for visual cards; lean on feature tests. (c) Candidate future task: revise the qa persona to judge on code + test results rather than un-runnable visual capture.

## Model strategy — judgment tier on llama3.3:70b, not gpt-oss (2026-05-31)

- **Local model inventory** (Spark Ollama, the performer pool): gpt-oss:120b/20b (harmony), qwen3.6:35b + qwen3-coder:30b + smaller coders (standard), **llama3.3:70b / llama3.1:70b (standard, 42GB)**, glm-4.7-flash:30b, qwen2.5:32b. LM Studio: gpt-oss-120b/20b, qwen3.6-35b-a3b. `local/*` is NOT a wildcard — only `local/qwen3-8b` + `local/qwen3-14b` (the coordinare's own internal inference, `COORDINARE_INFERENCE_MODEL=local/qwen3-14b`), too small for roles.
- **Key insight:** every format/tool-call failure this round traces to the **harmony** response format (gpt-oss): the openclaw `tool_calls` leak, the LiteLLM streaming bug (#17246/#13300), the assessor `reasoning_content` trip. `llama3.3:70b` uses **standard tool-calling** → retires that whole bug class, and is lighter (42GB vs gpt-oss:120b 65GB).
- **Final tiering (superseding the gpt-oss:120b qa staging):**
  - **Judgment (reviewer, qa) → `llama3.3:70b`.** reviewer: openclaw, Ollama-direct, `gpt-oss:120b`→`llama3.3:70b`. qa: claude_code via LiteLLM shim, `spark/llama3.3:70b`. Shared weights → Ollama loads llama3.3:70b once for both. (With a non-harmony model the reviewer could rejoin LiteLLM safely; kept Ollama-direct for now.)
  - **Builders (architect/implementer/security) → `qwen3.6:35b`** (working + fast; `qwen3-coder:30b` is an A/B candidate, not adopted).
  - **Assessor → `gpt-oss:20b`** Ollama-direct (format task, 20b sufficient — *not* "too small" here).
  - **Mechanical (closer/env_bootstrap/tech_writer) → `qwen3.6:35b`** (unchanged; swap-on-failure only).
- Applied as config-only edits (no restart) → take effect on the next natural restart; card #93 finishes on its current config without a rewind.

## llama3.3:70b judgment-tier experiment — FAILED, reverted to gpt-oss:120b (2026-06-01)

- Tried `llama3.3:70b` for reviewer + qa (hypothesis: non-harmony model dodges the tool_calls/format bugs). **Reviewer failed the JSON review contract 3/3** (`BACKEND_FORMAT_ERROR … max_retries_exceeded attempts=3`), blocking card #93 at reviewing.
- **Raw failing output was not a malformed verdict — it was off-task entirely:** *"To reduce the margins … we can use the CSS selector `.contact-card { margin: 0; }` …"* — i.e. llama answered the card title as a how-to instead of reviewing the diff and emitting JSON. A parser fix is therefore useless (nothing to parse).
- **Context-truncation hypothesis tested and DISPROVEN.** Suspected Ollama's small default `num_ctx` was truncating the persona. But `/api/show` → `llama.context_length=131072` and `/api/ps` after a fresh load → `context_length=131072` (full 128K), `vram=102.4GB`. So the prompt (~10K tok) had ample room; the persona was not dropped for size. Conclusion: **model fit**, not context — llama3.3:70b is chat-tuned and weak at agentic tool-use + structured-JSON review.
- **Bonus correction:** llama3.3:70b is NOT "lighter" — 42GB is weights only; at full 128K context its KV cache pushes it to **102GB VRAM** (vs gpt-oss:120b's 65GB), nearly filling the Spark's 121GB and risking context-shrink/offload under concurrency. gpt-oss:120b is the lighter *and* more capable pick for judgment.
- **Reverted:** reviewer → `gpt-oss:120b` (Ollama-direct), qa → `spark/gpt-oss:120b` (LiteLLM shim). gpt-oss's only defect (harmony tool_calls leak) is already neutralized by Ollama-direct / the 073 shim. The gpt-oss verdict-reliability flip-flop is a *separate*, grounding-level issue (NOT context truncation, since context is full) — to address via prompt/persona work, not a model swap.

## Assessor: gpt-oss:20b too weak for JSON contract → bumped to gpt-oss:120b (2026-06-01)

- gpt-oss:20b **deterministically** emitted a CORRECT assessment as prose (markdown bullets, no JSON object) and **blocked card #93 3/3** (`max_retries_exceeded`). Bumped assessor (junie provider + role) `gpt-oss:20b → gpt-oss:120b`. 120b emitted valid JSON and **cleared assessing → architecting** on the next run. No rebuild (config only).
- **Lenient-parse fallback — ATTEMPTED then REVERTED (safety).** Adding "non-JSON prose → treat as sufficient pass" to the assessor parse broke 5 performer tests, incl. `test_invalid_json_redacts_secrets_in_preview_and_reason`. The fail-closed behavior is DELIBERATE: assessor output can contain leaked secrets (`ghp_…`), and blindly accepting prose would (a) bypass the retry budget and (b) **commit leaked-secret prose as `assessment.md`**. The existing `_extract_json` already regex-extracts JSON from fenced/prose-wrapped output; the gap is only the no-JSON-at-all case, and a SAFE fallback there must fire only after retries, redact secrets first, and confirm a clear-pass — a security-relevant task, not a one-liner. Deferred; model bump is the safe path.

## Full lifecycle flowed on the gpt-oss:120b judgment tier (2026-06-01)

- Card #93 ran assess→architect→implement→review→security→qa cleanly on the settled tiering (assessor/reviewer/qa = gpt-oss:120b, builders = qwen3.6:35b). Reviewer (openclaw/120b) produced a **coherent, grounded APPROVED** ("correctly reduce margins … removing Bootstrap column gutters") — NO `BACKEND_FORMAT_ERROR`, no "no code present" flip-flop this run. qa engaged the contract and bounced the card back to implementing (rework), not stuck.

## STATE PERSISTENCE FIX — verified working + top-level sync (2026-06-01)

- After the daemon fix (persist on lifecycle-signature change, not just `phase`), live verification: snapshot `mtime` advances on **every** stage transition, and the persisted `active_sessions[card].performer_stage` tracks the **current** stage (`implementing`), matching live truth. Resume prefers the session stage (`check_board.py:40`), so **the restart-rewind is fixed**.
- Caught a latent gap: in shared-pool mode top-level `current_card`/`active_card_id` are None and top-level `performer_stage` stayed stale (`assessing`), which `handle_blocked.py` / `handle_system_error.py` read (defaulting to `assessing`). Tightened `_build_snapshot` to derive the top-level `performer_stage` from the active session via `_pick_stable_active_card_id` so those resume paths are correct too. Tests: `tests/unit/test_daemon_snapshot_persistence.py` (5 cases). Deploys on next restart (not restarting now — lifecycle is mid-flight).

## ENV-CACHE BOOTSTRAP — full failure chain + fixes (2026-06-01)

Triggered by a real QA finding: card #153's system spec (`type: :system`, `selenium_chrome_headless`) couldn't run — *"Headless Chrome is not installed in the CI environment."* Chasing it surfaced a chain of env-cache bootstrap bugs, all now fixed in this PR.

1. **Documentation gap (root cause).** The website README documented PostgreSQL/Redis/rbenv but **not** the Chrome/Selenium system-test dependency (it lived only in the CI workflow's `services: selenium/standalone-chrome` block). env_bootstrap reads `env_spec_files` (README) for what to install, so it had no signal. **Fix:** documented Chrome as an explicit dependency in the website README on `main` ([website#155](https://github.com/ViviDynamics/website/pull/155)).
2. **env_bootstrap endpoint silently dropped.** `env_bootstrap` is not a lifecycle stage, so the startup registration hit `unknown_role` and never added the opencode-ephemeral endpoint to `performer_services_by_id` → every re-bootstrap failed with `bootstrap_svc_not_found` → the cache was frozen at its last build. **Fix:** `_build_http_performer_services` now returns env_bootstrap endpoints in an id-keyed map merged into `performer_services_by_id`.
3. **Cache-reuse skipped the install.** A re-bootstrap against a populated cache short-circuited the install (sourced the cached `activate.sh`, ran only services-start/health). **Fix:** bootstrap persona mandates re-running the install idempotently on a populated cache.
4. **opencode hung on `permission.asked`.** opencode 1.15.x defaults to "ask"; headless `serve` has no approver → the agent hung ~16 min and the cache stayed untouched. **Fix:** the adapter's `opencode.json` sets `"permission": "allow"`.
5. **Broken install (silent).** With permission fixed, the agent executed but wrote `apt-get install chromium` (no `apt-get update`) into activate.sh — the base image strips apt lists, so it failed silently and Chromium never landed. **Fix:** persona directs system packages through the cache's `debs/` dir (apt-get update + download the dependency closure into `{cache}/debs`, install from local), matching how Postgres/Redis are handled.
6. **Silent success (the meta-bug).** Every layer trusted the one before — the bootstrap reported `env_bootstrap_complete` even though Chromium wasn't installed, so the coordinare marked the cache ready and recorded `readme_sha`, masking the failure. **Fix — "confirm installation before exiting" (verify-gate, both tiers, this PR):**
   - **Tier 1 (persona):** the agent must write `{cache}/verify.sh` asserting every documented dependency is present + runnable (actually invoke the tools), and must run it itself and not finish until it passes.
   - **Tier 2 (deterministic gate):** the env_bootstrap performer runs `verify.sh` after the agent turn (`workspace.run_env_cache_verify`); a non-zero exit returns `status=error` instead of `env_bootstrap_complete`. The coordinare's existing `on_bootstrap_complete(success=False)` path then clears `readme_sha` and retries — so a broken cache can never be marked ready. A missing verify.sh is degraded (logged), not fatal (legacy compatibility). Tests: performer `TestEnvBootstrapVerifyGate` (fail→error, pass→complete, missing→degraded) + persona assertions. (Generalizes the spec-078 health/smoke-test-gating idea to the env cache.)

Still-open (follow-up, noted not fixed): the `readme_sha`-recorded-on-dispatch path (`_do_dispatch`) still mis-records if a bootstrap *hangs and never completes* (no `on_bootstrap_complete` call) — the verify-gate fixes the silent-*success* case, not the never-terminates case (that's the stall watchdog's domain). And feeding the verify failure text into the next bootstrap persona (true self-correction) is a future refinement.

## DIAGNOSTIC ROLE + BROWSER-CONTROL MATRIX (2026-06-02)

**Why.** Confirming "can each backend actually write+run code to drive Chrome, and is Chrome present/accessible in the performer image?" had no clean test path. The performer Job API has **no free-form execution role** — every role wraps the persona in role-specific scaffolding that distorts an ad-hoc task:
- `implementing` → unconditionally `push_branch` + `create_pull_request`; a zero-commit task (screenshot to `/tmp`) dies with GitHub 422 "No commits between main and <branch>".
- `qa` → the agent rubber-stamps a JSON verdict (`qa_passed`, `criteria_checked: 0`) from reasoning **without executing** the task.
- `env_bootstrap` → runs the install/verify framing; with no manifest it does nothing (`files_modified: []`).

**Fix — new `diagnostic` role (first-class Job-API capability).** A free-form viability/benchmark probe: runs the agent agentically with the persona/task verbatim and full tooling, but with NONE of the lifecycle scaffolding (no JSON verdict, no commit/push/PR, no env-cache verify or service-inference). Returns the agent's output with `success=True`. Implementation:
- `protocol.py`: new terminal status `diagnostic_complete` (auto-non-failure → success).
- `models.py`: `DIAGNOSTIC_ROLE = "diagnostic"`.
- `main.py`: short-circuit branch in the `done` handler (before every role branch) returning `diagnostic_complete` with the agent output; resumed-state early return mirrors `env_bootstrap_complete`.
- 6 prompt builders (claude_code, codex, opencode, junie, hermes, pi; openclaw reuses opencode's; opencode_compat too): first-branch free-form tail ("one-off diagnostic task; use any tools; do NOT commit/push/PR").
- Tests: `test_protocol.py::TestDiagnosticStatus`, `test_claude_code.py::...test_diagnostic_role_uses_free_form_footer`.
- Harness: `scripts/smoke_browser.py` (role=diagnostic, sequential; ground truth = a valid PNG on disk via `docker exec stat` + PNG magic-byte check; full job result dumped to `tmp/smoke_shots/<eid>.json`).

**Build-context gotcha (corrected).** The performer **Python source is COPYed in `Dockerfile.base`** (`COPY agent/performer/src/ ./src/`, repo-root paths), NOT in `Dockerfile.full`. So `:base` must build with context `.` (repo root) and `:full` with context `agent/performer/`. Building only `:full` silently ships stale code. (The restart-coordinare skill's "context = agent/performer/" applies to `:full` only.)

**Matrix result — screenshot vividynamics.com (6/7 drove Chrome):**

| endpoint | backend | model | result |
|---|---|---|---|
| opencode | opencode | gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| junie | junie | gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| openclaw | openclaw | gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| hermes | hermes | spark/qwen3.6:35b | ✅ 1.6 MB full-page PNG |
| pi | pi | spark/qwen3.6:35b | ✅ 1.6 MB full-page PNG |
| codex | codex | spark/qwen3.6:35b | ✅ 0.9 MB PNG — **viewport-only 1280×720** (agent omitted `full_page=True`) |
| claude_code | claude_code | spark/qwen2.5-coder:14b-instruct | ❌ no run — `400 "qwen2.5-coder...does not support thinking"` |

**Findings.**
- **Chrome is present + accessible** in `coordinare-performer:full` (Playwright + Chromium); 6 distinct backends wrote+ran Playwright code to drive it across **two model families** (gpt-oss:120b, qwen3.6:35b).
- **claude_code + non-reasoning model is incompatible**: the Claude Code CLI (via the LiteLLM shim) emits an extended-thinking request that Ollama's `qwen2.5-coder:14b-instruct` rejects with a 400 — the agent never gets a turn. This is why earlier `qa`-role smokes "passed" on this combo (the verdict was a rubber-stamp, no real turn). To run claude_code on self-hosted models, route it to a thinking-capable model or disable thinking at the shim.
- **codex viewport-only**: codex produced a valid screenshot but didn't honor `full_page=True` — a prompt-adherence nuance on the small model, not a capability gap.

## FAIR RE-RUN — ALL BACKENDS ON gpt-oss:120b (2026-06-02)

The first matrix was not apples-to-apples (claude_code was handicapped on a
non-thinking model). Re-ran with every backend pinned to the **most capable
Spark model, gpt-oss:120b**, via a generated `config.fairtest.yaml` (gitignored;
prefix-preserving swap: LiteLLM backends → `spark/gpt-oss:120b`, Ollama-direct →
`gpt-oss:120b`). Harness: `scripts/smoke_browser.py --config config.fairtest.yaml`.

| backend | route | result |
|---|---|---|
| claude_code | LiteLLM `spark/gpt-oss:120b` | ✅ 1.6 MB full-page PNG (1280×1480) — **handicap removed** |
| opencode | Ollama-direct gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| junie | Ollama-direct gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| openclaw | Ollama-direct gpt-oss:120b | ✅ 1.6 MB full-page PNG |
| hermes | LiteLLM `spark/gpt-oss:120b` | ✅ 1.6 MB full-page PNG |
| pi | LiteLLM `spark/gpt-oss:120b` | ✅ 1.6 MB full-page PNG |
| codex | LiteLLM `spark/gpt-oss:120b` | ⚠️ did NOT drive Chrome — used the external **Thum.io** image service, saved a **GIF mislabeled `.png`**. PNG magic-byte check caught it. |

**Conclusions.**
- **claude_code's earlier failure was 100% the model**, not the backend. The Claude Code CLI emits an extended-thinking request; on a thinking-capable model (gpt-oss:120b) it runs the full agentic loop and drives Chrome cleanly (it even reported "screenshot has been captured successfully... 1,635,302 bytes valid PNG"). On a non-reasoning model (qwen2.5-coder) the request 400s and the agent never starts. **Takeaway: route claude_code only to thinking-capable models on the Spark.**
- **6/7 backends write+run Playwright to drive the in-image Chrome** when given the same capable model — confirming Chrome is present/accessible and the agentic browser-driving path works across heterogeneous backends.
- **codex diverges to a third-party screenshot API** (Thum.io) rather than driving the local browser, despite the persona explicitly instructing Playwright use. This is a model/backend prompt-adherence finding — codex "solved" the task without demonstrating local-Chrome driving (and produced a non-PNG). The strict valid-PNG gate is what surfaced it; a laxer "file exists" check would have scored it a false pass.
- **Lesson for fair multi-backend benchmarking:** hold the model constant. Per-route model-name prefixing (`spark/` vs bare) must match each backend's provider URL, and two backends (junie `JUNIE_PROVIDER_MODEL`, hermes `HERMES_MODEL`) pin the model in endpoint env rather than reading the dispatch payload — both must be overridden for a true single-model run.

## gpt-oss:20b — CLEAN 7/7 SWEEP (2026-06-02)

Same fair setup, all backends pinned to **gpt-oss:20b** (`config.fair20b.yaml`,
gitignored). Result: **7/7 backends produced a real, valid PNG** (confirmed via
`file` — all "PNG image data", no GIF/format cheats).

| backend | result |
|---|---|
| claude_code, opencode, junie, openclaw, hermes, pi | ✅ valid full-page PNG |
| codex | ✅ **valid 2 MB PNG via real Playwright** — did NOT use Thum.io this time |

**Notable:** codex drove the local Chrome correctly on gpt-oss:20b (progress:
"Screenshot captured and saved... 2091970 bytes"), whereas on gpt-oss:120b it
diverged to the external Thum.io API and saved a GIF. So the **larger model was
*worse* for this task's prompt-adherence** — a reminder that bigger ≠ more
contract-faithful (echoes the qwen3.6:35b architect "plan-don't-implement"
finding). claude_code passes here too (gpt-oss:20b is thinking-capable). pi's
progress explicitly described the Playwright script it wrote+ran.

## FULL MATRIX — 6 MODELS × 7 BACKENDS (browser-control diagnostic, 2026-06-02)

Every cell = one `diagnostic`-role job asking the backend to screenshot
vividynamics.com via Playwright. Model held constant per run (fair configs via
`scripts/gen_fair_config.py`; route-prefix preserved). Legend: ✅ drove Chrome →
valid PNG · ⚠️ "done" but did NOT drive local Chrome · ✗ no valid PNG (reason).

| backend ↓ / model → | gpt-oss:120b | gpt-oss:20b | qwen3.6:35b | qwen3-coder:30b | qwen2.5:32b | qwen2.5-coder:14b | score |
|---|---|---|---|---|---|---|---|
| claude_code | ✅ | ✅ | ✅ | ✗ no-thinking | ✗ no-thinking | ✗ no-thinking | 3/6 |
| opencode | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ echoed prompt | 5/6 |
| pi | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ described only | 5/6 |
| hermes | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ no file | 5/6 |
| junie | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ Junie build err | 5/6 |
| openclaw | ✅ | ✅ | ✅ | ✅ (job status quirk) | ✗ wrote script, didn't run | ✗ NO_REPLY | 4/6 |
| codex | ⚠️ Thum.io GIF | ✅ | ✅ | ✅ | ✗ empty output | ✗ no exec | 3/6 real |
| **model score** | **6/7** | **7/7** | **7/7** | **6/7** | **4/7** | **0/7** | |

**Headline conclusions.**
- **claude_code is thinking-gated.** It fails on *every* non-reasoning model
  (`400 ...does not support thinking`) and succeeds on *every* reasoning model
  (gpt-oss:120b/20b, qwen3.6:35b). Deterministic — route claude_code only to
  thinking-capable Spark models.
- **Best all-round models:** gpt-oss:20b and qwen3.6:35b both swept 7/7 (every
  backend drove Chrome). qwen3.6:35b is slow (~85s/turn) but contract-faithful.
- **Bigger ≠ better.** gpt-oss:120b scored *below* gpt-oss:20b — codex skipped
  Playwright and called the external Thum.io API (GIF mislabeled .png) only on
  120b. And qwen2.5:32b (20 GB) badly underperformed the smaller qwen3 models.
- **qwen2.5-coder:14b is unusable for agentic browser work: 0/7.** Every backend
  either echoed the prompt, only described steps, returned NO_REPLY, or hard-
  failed — the model is too weak to execute the tool loop, regardless of backend.
- **Most robust backends:** opencode / pi / hermes / junie (5/6 — only the 14B
  coder beat them). openclaw 4/6. codex 3/6 (one external-service cheat + two
  weak-model misses). claude_code 3/6 (purely thinking-gated, not capability).
- **A "file exists" check would have over-counted** codex on gpt-oss:120b (GIF)
  and openclaw on qwen2.5:32b — the strict valid-PNG magic-byte gate is load-
  bearing for honest scoring.
