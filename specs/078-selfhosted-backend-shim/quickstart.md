# Quickstart: Self-Hosted Backend Robustness Layer

## What it does

Makes self-hosted-routed agent CLIs behave like their native cloud. Activates
**only** when a `(backend, model)` pair has a routing-table entry; otherwise a
byte-for-byte no-op.

## Configure a target (operator)

Add a `selfhosted_routing` entry (see `contracts/routing-table.md`):

- **Reroute** around a broken layer (e.g. openclaw → Ollama-direct):
  `strategy: reroute`, `base_url:` the clean upstream, no normalizers.
- **Normalize** a lossy layer (e.g. codex on LiteLLM gpt-oss):
  `strategy: normalize`, `normalizers: [harmony_tool_calls]`, and a
  `reroute_upstream:` clean fallback for auto-reroute on probe failure.
- **Strip reasoning** (claude_code on a qwen reasoner): `normalizers: [strip_reasoning]`.

A `(backend, model)` with **no entry** → native cloud → layer does nothing.

## Verify (developer)

Run the performer suite (separate from coordinare — conftest collision):

```bash
.venv/bin/pytest agent/performer/tests/unit/proxy/ -q
```

Key checks mapped to Success Criteria:

| Test | Asserts | SC |
|---|---|---|
| `test_routing.py` | missing `(backend, model)` → no proxy, no env override | SC-003 |
| `test_normalizer_harmony.py` | leaked `<\|channel\|>` JSON+SSE → structured `tool_calls`, no markers | SC-001/002 |
| `test_normalizer_reasoning.py` | reasoning blocks stripped JSON+SSE (073 regression) | SC-002 |
| `test_shim.py` | only declared normalizers run; unknown format passes through | SC-006 |
| `test_health.py` | healthy→proceed; unhealthy+reroute→rerouted; unhealthy→fail-closed; probe timeout→unhealthy | SC-005 |
| `test_launch.py` | reroute repoints provider env to clean upstream, no normalizer | SC-004 |

Lint:

```bash
.venv/bin/ruff check agent/performer/src/performer/proxy/
```

## Integration smoke (manual)

Point a tool-using backend at a self-hosted gpt-oss target with
`strategy: normalize, normalizers: [harmony_tool_calls]`, dispatch a card, and
confirm the agent reads/edits a file (the 077 openclaw failure no longer
reproduces). Then flip the same target to `strategy: reroute` at the clean Ollama
upstream and confirm the provider env points there with no shim in path.
