# Contract: Routing Table — `translate` Strategy

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08
**Source FRs**: FR-006 (per-entry translating choice), FR-007 (per-pair activation, no regression),
FR-008 (reject contradictory wire/strategy at load), FR-009 (fail-closed on broken table)

This contract extends the 078 routing-table schema (`routing.example.yaml`) with the `translate`
strategy value. It is the operator-facing surface; the loader (`proxy/routing.py`) enforces it at job
start and **fails closed** on any violation (FR-009 / FR-078-5).

---

## Schema delta

A routing entry maps a `(backend, model)` pair to a `TargetDescriptor`. The only change is one new
legal value for `strategy`:

```yaml
# Strategy ∈ {normalize, reroute, translate}   # was {normalize, reroute}
```

### `translate` entry shape

```yaml
routes:
  - backend: claude_code           # the Anthropic-wire backend (the CLI POSTs /v1/messages)
    model: "gpt-oss:120b"          # the self-hosted model key the role resolves to
    target:
      base_url: "http://192.168.3.30:11434/v1"   # OpenAI-wire upstream (Ollama-direct)
      wire_format: openai          # REQUIRED to be `openai` for translate (see Rule T1)
      strategy: translate          # NEW
      normalizers:                 # OPTIONAL; same registry rule as `normalize`
        - harmony_tool_calls
        - strip_reasoning
      reroute_upstream: null       # OPTIONAL health-gated fallback (FR-010)
```

---

## Load-time validation rules

The loader applies these in addition to the existing 078 rules. Any failure aborts job start with an
**actionable** message (names the offending `(backend, model)` pair and the rule). No partial/degraded
load (FR-009).

| Rule | Condition | On violation |
|------|-----------|--------------|
| **T1** (FR-008) | `strategy == "translate"` ⇒ `wire_format == "openai"` | Reject: `translate` + `wire_format: anthropic` is contradictory — there is nothing to translate when the upstream already speaks Anthropic. Message names the pair and both fields. |
| **T2** (FR-008) | `strategy == "translate"` ⇒ every key in `normalizers` is registered in `NORMALIZER_REGISTRY` | Reject with the unknown key(s) listed. Empty/absent `normalizers` is legal (translation alone). |
| **T3** | `base_url` present, non-empty, parses as a URL | Reject (unchanged 078 rule). |
| **T4** (unchanged) | `strategy == "reroute"` ⇒ `normalizers` empty/absent | Reject (unchanged). |
| **T5** (unchanged) | `strategy == "normalize"` ⇒ `normalizers` non-empty, all registered | Reject (unchanged). |
| **T6** (FR-009) | The table file is missing, unreadable, or not valid YAML/schema | Fail closed at job start; do not launch the job. |

### Non-regression (FR-007)

- A `(backend, model)` pair with **no** entry is a byte-for-byte no-op — no shim, no translation.
  Existing `reroute`/`normalize` entries for OpenAI-wire backends (openclaw, junie, codex, pi) load and
  behave **identically** to pre-084. The only new behavior is gated behind `strategy: translate`.
- Adding a `translate` entry for `(claude_code, <model>)` MUST NOT alter resolution for any other pair.

---

## Worked examples

### Valid — claude_code over Ollama-direct gpt-oss, full cleanup

```yaml
- backend: claude_code
  model: "gpt-oss:120b"
  target:
    base_url: "http://192.168.3.30:11434/v1"
    wire_format: openai
    strategy: translate
    normalizers: [harmony_tool_calls, strip_reasoning]
```

### Valid — translation only, no normalizers

```yaml
- backend: claude_code
  model: "gpt-oss:20b"
  target:
    base_url: "http://192.168.3.30:11434/v1"
    wire_format: openai
    strategy: translate
    normalizers: []
```

### Invalid — Rule T1 (contradictory wire/strategy)

```yaml
- backend: claude_code
  model: "gpt-oss:120b"
  target:
    base_url: "http://host/v1"
    wire_format: anthropic     # ✗ load error: translate requires wire_format: openai
    strategy: translate
```

### Invalid — Rule T2 (unregistered normalizer)

```yaml
- backend: claude_code
  model: "gpt-oss:120b"
  target:
    base_url: "http://host/v1"
    wire_format: openai
    strategy: translate
    normalizers: [harmony_tool_calls, made_up_filter]   # ✗ load error: 'made_up_filter' not registered
```
