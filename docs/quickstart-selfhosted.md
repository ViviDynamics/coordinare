# Self-hosted backend quickstart

The `opencode_compat` performer backend points the coordinare at any
OpenAI-compatible `/v1/chat/completions` endpoint — LM Studio, vLLM, Ollama, or
a LiteLLM proxy — without code changes. It speaks an LCD (lowest-common-
denominator) profile of the OpenAI wire format: no hosted-tool descriptors, no
`developer` role, no `prompt_cache_key`, no other OpenAI-only extensions.

**Authoritative source**: the operator walk-through lives in
[`specs/067-compatibility-first-backend/quickstart.md`](../specs/067-compatibility-first-backend/quickstart.md).
This document is the public-facing copy for operator distribution; if it ever
drifts from the spec, the spec wins.

## TL;DR

```yaml
performers:
  implementer:
    backend: opencode_compat
    base_url: http://localhost:1234/v1
    model: qwen3-coder-30b
    compat_remap_developer_role: true   # safety net; default true
```

1. Start LM Studio (or vLLM / Ollama / LiteLLM) serving an OpenAI-compatible
   `/v1/chat/completions` endpoint with a model whose context window is at
   least 32 KiB.
2. Set `backend: opencode_compat` on the performer roles you want routed to
   the self-hosted endpoint.
3. `set -a && source .env && set +a && .venv/bin/python -m coordinare`.

See the spec quickstart for the full step-by-step and the SC-003 swap test.

## Troubleshooting

When upstream returns a non-2xx response, the daemon log emits a single
`WARN` (transient) or `ERROR` (permanent) line containing every field of the
upstream `UpstreamHTTPError` envelope verbatim — `status`, `route`, stripped
`base_url`, the upstream `upstream_body`, `body_truncated`, `elapsed_ms`, and
`upstream_request_id`. Grep for `upstream_http_error` in the daemon log to
locate the failure.

Transient statuses (`408`, `429`, `500`, `502`, `503`, `504`) and body markers
(`temporarily unavailable`, `rate limit`) trigger retry; everything else
surfaces immediately. The classification list lives in
`src/coordinare/graph/nodes/handle_system_error.py` and is the single source of
truth (FR-005).
