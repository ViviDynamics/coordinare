# Research: Route the hermes (tech_writer) Backend Through the Self-Hosted Shim

## D0 — What actually breaks hermes (live probe, 2026-06-21)

Two direct probes of hermes's real upstream (`spark/gpt-oss:120b` via `https://litellm.vividynamics.com/v1`, the exact endpoint+model+key hermes uses) with a JSON-contract prompt returned: (1) a body containing **invalid control characters** (JSON parse failed at char 199), and (2) an **empty body** (0 bytes — overload). These are the **exact failure shapes spec-098 fixed for the assessor** (control-chars + empty/overload), NOT markdown-fenced or prose-wrapped JSON.

**Implication**: hermes needs the **same** normalizer chain as junie — `strip_control_chars` (098) + `strip_reasoning` (#130) — plus the 099 completion health probe (for the empty-body/overload gating). **No new normalizer is required.** The root cause is confirmed to be the missing normalization on the hermes path, not a model-capability or a novel output shape.

## D1 — Make hermes eligible for the shim

**Decision**: Remove `"hermes"` from `UNSUPPORTED_BACKENDS` and add `"hermes": "HERMES_BASE_URL"` to `PROVIDER_BASE_URL_ENV` in `proxy/launch.py`. The launch seam then repoints `HERMES_BASE_URL` at the shim loopback exactly as it does the other backends' provider envs.

**Rationale**: hermes reads `HERMES_BASE_URL` as its OpenAI-compatible provider base (writes it into the hermes-agent `model: {provider: custom, base_url: …}` config). It was excluded only because it wasn't wired into the layer — not for any incompatibility.

**Alternatives rejected**: a hermes-specific shim — duplicates the existing `SelfHostedShim`; reuse it.

## D2 — Loopback base wire-path: `/v1` prefix, not junie's verbatim full path

**Decision**: When routing hermes, the loopback base handed to `HERMES_BASE_URL` must be `<shim-loopback>/v1` (the shim serves `/v1/chat/completions`; the hermes-agent custom-provider CLI **appends `/chat/completions`** to its base). This differs from junie, which posts **verbatim** to the full path (`<loopback>/v1/chat/completions`) because junie appends nothing.

**Rationale**: two distinct CLI conventions → two distinct base constructions. junie = full served path (it POSTs verbatim); hermes = `/v1` prefix (it appends `/chat/completions`). Keep `VERBATIM_POST_WIRE_PATH` (junie → `/v1/chat/completions`) as-is and add a separate per-backend base-prefix for hermes (`/v1`), applied where the loopback env is set. A wrong base surfaces at the startup completion probe (which exercises the same path) → unhealthy/fail-closed, never a silent mid-job 404.

**Alternatives rejected**: making the shim also serve bare `/chat/completions` — widens the shim's surface for one backend; the `/v1` prefix is local to hermes's launch wiring.

## D3 — Health probe: completion mode (099)

**Decision**: The hermes routing target declares `health_probe: completion` (099). tech_writer is a non-tool-calling JSON-completion role; the default tool-call probe would gate it unhealthy → fail-closed every documenting job. The completion probe judges a non-empty **normalized** completion as healthy and fail-closes on the empty/overloaded body shape (which the live probe showed is real).

**Rationale**: identical to why junie needed 099. Reuse it.

## D4 — Normalizer chain: reuse, no new normalizer

**Decision**: hermes routing entry declares `normalizers: [strip_control_chars, strip_reasoning]` — the same chain junie uses. `strip_control_chars` (098) removes the invalid control bytes that broke the parse; `strip_reasoning` (#130) promotes a reasoning-only answer into content / strips the reasoning channel. Fail-open on already-clean output.

**Rationale**: D0's probe showed the failing shapes are control-chars + empty body — exactly what these normalizers + the completion probe handle. Adding a markdown-fence/JSON-extraction normalizer is **deferred** (no evidence of fenced output); if fences are later observed, that's a separate, additive normalizer.

## D5 — Default-safe / opt-in

**Decision**: With no routing entry naming hermes, `maybe_launch_proxy` resolves no target → byte-for-byte no-op (hermes talks its configured provider directly, as today). No other backend's path changes. Activation = adding the hermes routing entry + mounting `routing.yaml` into hermes-ephemeral (config/ops, not code).

**Rationale**: matches the layer's established opt-in contract (FR-078-1); the eligibility change is inert until an operator adds the entry.

## D6 — Routed target must be a no-auth upstream (probe sends no auth) [adversarial-review finding]

**Decision**: Route hermes at the **bare Ollama origin** (`http://192.168.3.30:11434`), NOT the LiteLLM endpoint. The startup health probe (`check_health`) POSTs to `target.base_url` with **no Authorization header**; an auth-required upstream (LiteLLM) would 401 → unhealthy → fail-closed, blocking every hermes job. Every other routed target (junie, openclaw reroute, claude-qa translate) already points Ollama-direct (no auth) for the same reason — gpt-oss:120b is served Ollama-direct, and hermes's `HERMES_API_KEY` is ignored by Ollama. tech_writer dispatches `gpt-oss:120b` (Ollama model name) via mode `single-gptoss120-ollama`, matching the routing entry; no `upstream_model` needed.

**Rationale**: surfaced by the pre-merge adversarial review — the probe's no-auth assumption held for all prior (Ollama-direct) targets but would break a LiteLLM-fronted hermes. Routing Ollama-direct is consistent, auth-free, and the same proven path junie uses. Forwarding auth into the probe is a broader layer change, deferred (out of scope; no routed target needs it today).

**Constraint recorded**: a `normalize`/`translate` routing target MUST be a no-auth upstream (or declare `reroute_upstream` for the unhealthy path) until the probe learns to forward credentials.
