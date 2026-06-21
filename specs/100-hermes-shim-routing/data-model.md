# Data Model: Route the hermes (tech_writer) Backend Through the Self-Hosted Shim

**No persisted coordinare state and no schema change.** All changes are to the
performer-side proxy launch wiring + a config (routing-table) attribute.

## 1. Proxy launch eligibility (code constants)

| Constant (`proxy/launch.py`) | Before | After |
|---|---|---|
| `UNSUPPORTED_BACKENDS` | `{"hermes"}` | `frozenset()` (hermes removed) |
| `PROVIDER_BASE_URL_ENV` | (no hermes) | + `"hermes": "HERMES_BASE_URL"` |
| loopback base for hermes | n/a (excluded) | `<shim-loopback>` + `/v1` prefix (CLI appends `/chat/completions`) |

junie's `VERBATIM_POST_WIRE_PATH` (`/v1/chat/completions`, posted verbatim) is unchanged; hermes's `/v1` prefix is a separate per-backend base case.

## 2. hermes routing entry (operator config — the `SELFHOSTED_ROUTING_CONFIG` YAML)

```yaml
- backend: hermes
  model: gpt-oss:120b        # operator-owned; the Ollama-direct model name
  target:
    base_url: http://192.168.3.30:11434   # bare Ollama origin, no auth (probe sends none)
    wire_format: openai
    strategy: normalize
    health_probe: completion        # 099 — non-tool-calling JSON role
    normalizers: [strip_control_chars, strip_reasoning]   # 098 chain (reused)
```

With NO such entry, hermes is unchanged (opt-in).

## 3. Reused mechanisms (unchanged)

- `SelfHostedShim` (normalize mode) + served front-door paths (`/v1/chat/completions`).
- `TargetDescriptor.health_probe` + the completion probe + `gate()` (099).
- `strip_control_chars` (098), `strip_reasoning` (#130) normalizers.
- `maybe_launch_proxy` / `_launch_for_target` normalize branch + provider-env repoint.

## 4. Observability (reused, secret-free)

The `proxy.health` decision record (mode / status / resolved_action / path / wire_format / strategy) and normalizer behavior carry only non-secret fields — never tokens or raw model output (FR-006). No new record types.
