# Research: Reasoning Policy and Truncation Classification (163)

Findings from 2026-09-04/05. R1-R3 were obtained by **executing** against the live gateway
or by reading the actual source; quoted output is real. All five are resolved.

## R1: Is the failure real on the model in use today?

**Decision**: Yes. It is not a quirk of a retired model.

**Evidence** (through the gateway, `spark/glm-5.3-flash`, a JSON-only-shaped prompt):

```
budget=64    finish=length   content=0 chars    reasoning=303 chars   <-- EMPTY CONTENT
budget=128   finish=length   content=0 chars    reasoning=550 chars   <-- EMPTY CONTENT
budget=512   finish=stop     content=332 chars  reasoning=1614 chars
```

**Rationale**: The ticket (#244) framed this as a capability gap in `qwen3.6:35b`, and that
framing survived into the record. It is not: the same shape reproduces on the current
model. What makes it latent rather than active is only budget headroom — roles are
configured 8192-32768, and at 2048 this model still returns valid JSON. A context-heavy
role on a long prompt is where it would bite.

**Alternatives considered**: Treating it as historical and closing #244. Rejected on the
measurement — the shape is model-independent, and spec 119 already shows the misdiagnosis
costs real time when it fires.

## R2: Can reasoning be controlled per request, and is one flag enough?

**Decision**: Yes it can, and no it is not. The policy must be **per model**, defaulting to
no override.

**Evidence** (`chat_template_kwargs: {"enable_thinking": false}`, through the gateway):

| Model | reasoning chars | content | verdict |
| --- | --- | --- | --- |
| `ada/qwen3-14b` | 600 → **0** | `{"ok":true}` | clean, parseable |
| `ada/qwen3-8b` | → **0** | `{"ok":true}` | clean, parseable |
| `spark/glm-5.3-flash` | 168 → **0** | `The user has asked me to reply only with the JSON…` (188 and 298 chars over two runs) | **does NOT parse** |

`reasoning_effort: "low"` was also tried: ignored by these models (reasoning 500 chars,
unchanged behaviour).

**Rationale**: On `glm-5.3-flash` the flag does not remove thinking — it removes the
*separation*, moving the thinking into `content`. Baseline behaviour there is already
correct (reasoning cleanly separated, content valid JSON). Applying the flag would
**manufacture** the exact failure this feature exists to prevent, on the model every role
currently uses. That single measurement is the whole argument for US3.

**Alternatives considered**: A global request parameter. Rejected — it would break the
default model. A role-level setting. Rejected — the correct value depends on the model, not
the role, so a role-level knob would need updating every time a role's model changes.

## R3: Can the CLI backends carry the flag?

**Decision**: No. This is what forces the injection point question in R4 — which R4 then answers in favour of the shim.

**Evidence**:

- `chat_template_kwargs` is a **vLLM-specific body extension**, not part of the
  OpenAI-compatible surface these CLIs implement.
- `codex`: coordinare builds `~/.codex/config.toml` from `CODEX_PROVIDER_*` with keys
  `model_provider`, `name`, `base_url`, `env_key`, optional `wire_api`. The builder
  validates and quotes specifically **so extra TOML keys cannot be injected** — its own
  docstring says so. Even if codex supported more, this writer forbids it.
- `openclaw`: coordinare writes `~/.openclaw/openclaw.json` with a fixed provider schema
  (`baseUrl`, `apiKey`, `api`, `timeoutSeconds`, `models[]` of
  `{id, name, reasoning, input, cost, contextWindow, maxTokens}`). No arbitrary-body hook.
  The per-model `reasoning` field is OpenClaw's own client-side capability declaration, not
  a request parameter.
- No `extra_body` / `defaultParams` / passthrough hook is documented anywhere in the repo
  for either CLI.

**Rationale**: Coordinare does not construct these requests; the CLI does. A parameter it
does not model cannot be added from outside.

**Alternatives considered**: Per-CLI hooks if any exist. Rejected even if found — it would
be a different mechanism per backend, needing separate verification each time, which is the
interface inconsistency this design is trying to avoid.

## R4 (RESOLVED — US2 is unblocked): does the shim reach the roles that need it?

**Decision**: **Yes.** Answered from the code and the live routing config; no experiment
needed. An earlier draft of this plan called for a live openclaw dispatch to settle it. That
was unnecessary caution — the evidence was already in the tree.

**The chain**:

1. `openclaw` is in `PROVIDER_BASE_URL_ENV` (`launch.py`), mapped to
   `OPENCLAW_PROVIDER_BASE_URL`, so the launcher can repoint it at a shim.
2. `UNSUPPORTED_BACKENDS` is `frozenset()` — **empty**. Nothing is excluded from proxying.
3. `launch.py` names openclaw explicitly among the CLIs that append their own path and
   correctly take the bare loopback root: *"CLIs that append their own path (codex,
   opencode, openclaw, pi, claude_code) are absent here and keep the bare root."*
4. So openclaw POSTs to `<loopback>/chat/completions`, and `_FRONT_DOOR_PATHS` serves
   exactly that: `("/v1/messages", "/v1/chat/completions", "/messages", "/chat/completions")`.
5. `shim.py`'s own comment identifies the historical failure and its fix: the un-prefixed
   variants are registered because *"an OpenAI-wire CLI whose (repointed) provider base_url
   is the bare loopback origin POSTs to `/chat/completions` (no `/v1`), which would
   otherwise 404 at the router before the handler ran (**122: the shared cause of the
   openclaw + opencode shim failures**)"*. Added in `a63fe63`.
6. **The corroboration that settles it**: `opencode` — the *other* backend named in that
   same comment as sharing the identical cause — routes through the shim in production
   today (`strategy: normalize` in `routing.yaml`). The fix is demonstrably working for a
   backend with the same failure mode.

**So the `routing.yaml` note is stale inside its own spec.** Spec 122 created the openclaw
exception AND fixed the 404 that motivated it; opencode was switched back and openclaw was
not. The workaround outlived its cause by one commit.

**codex**: no recorded blocker at all. Never mentioned in any routing file, present in
`PROVIDER_BASE_URL_ENV`, and named in the same bare-root list as openclaw. The same path
applies.

**Consequence for US2**: take the uniform option. Route the four direct backends
(reviewer/security on openclaw, architect/implementer on codex) through the shim, making it
the single injection point for every backend, and retire the stale exception. US2 is
**unblocked**.

**Residual risk, stated honestly**: this is a code-and-config argument, not an observed
dispatch. It is strong — the mechanism is present, the exclusion list is empty, the path is
served, and a same-cause sibling works in production — but the first shim-routed openclaw
dispatch is still the moment to watch. That is implementation verification, not a
precondition for designing the work.

## R5: Where do the two behavioural traps live?

**Decision**: Classification must be separated from the two derivations built on it. See
plan.md for the full treatment; recorded here so the finding is not lost if the plan is
rewritten.

**Evidence** (read from source):

- `handle_system_error.py` ~138: `is_env_blocked = assessor_shape == "empty_body"` —
  ungating hands the spec-095 ENV_BLOCKED operator state to every stage.
- `monitor_performer.py` ~4541: the retry gate includes `or assessor_shape is not None` —
  ungating makes every shape, including `malformed_body`, retryable for every stage,
  generalising a deliberate spec-119 decision.

**Rationale**: Both read as incidental conditions and are load-bearing. "Ungate the
classifier" is a two-line change that quietly rewrites two operator-facing behaviours.
