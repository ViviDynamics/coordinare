# Contract — `UpstreamHTTPError` envelope

Cross-boundary contract between the performer and the coordinare for non-2xx responses from the model endpoint. Pinned here so that a future performer change cannot silently drop fields the coordinare still depends on (the 044/067 family of incidents).

## Direction

Performer → coordinare, embedded inside `PerformerResponse.metrics.upstream_http_error` (existing optional `metrics` extension slot on `protocol.py`).

## Schema (JSON form on the wire)

```json
{
  "kind": "upstream_http_error",
  "status": 400,
  "body": "{\"error\":\"context length exceeded\"}",
  "body_truncated": false,
  "route": "POST /v1/chat/completions",
  "base_url": "http://localhost:1234/v1",
  "upstream_request_id": null,
  "elapsed_ms": 142,
  "occurred_at": "2026-05-20T18:14:09.221Z"
}
```

## Field rules

| Field | Required | Constraint |
|-------|----------|------------|
| `kind` | yes | Literal `"upstream_http_error"`. Used as a discriminator if the metrics slot ever carries multiple envelope types. |
| `status` | yes | Integer ≥ 100 and ≤ 599. |
| `body` | yes | UTF-8 string. Length ≤ 2048 bytes after truncation suffix. |
| `body_truncated` | yes | Boolean. `true` iff the raw upstream body exceeded `BODY_CAP_BYTES - len(TRUNCATION_SUFFIX)`. |
| `route` | yes | HTTP method + path, space-separated. |
| `base_url` | yes | Endpoint base URL with userinfo / `api_key` query param stripped. |
| `upstream_request_id` | no | Mirrors `x-request-id` / `openai-request-id` header if present. |
| `elapsed_ms` | yes | Integer ≥ 0. |
| `occurred_at` | yes | ISO-8601 UTC timestamp with `Z` suffix. |

## Truncation

`BODY_CAP_BYTES = 2048` and `TRUNCATION_SUFFIX = "...<truncated>"` are part of this contract — downstream consumers (logs, UI, classifier) may rely on the cap to bound memory and on the suffix as a sentinel. Raw upstream bodies exceeding `BODY_CAP_BYTES - len(TRUNCATION_SUFFIX)` bytes are cut at that boundary and the suffix appended, with `body_truncated = true`. Multi-byte UTF-8 sequences split at the cut are repaired (no partial code points on the wire).

## Backward-compatibility

- The envelope is **additive** — older coordinares that don't know the field drop it (pydantic v2 `extra="ignore"` on `PerformerResponse.metrics`).
- The coordinare MUST tolerate an absent envelope and fall back to the previous failure path (this preserves behaviour against the existing `codex` backend, which does not emit this envelope yet — out of scope for 067).

## Contract test

`tests/contract/test_067_upstream_http_error_schema.py` MUST:
1. Round-trip every field through `pydantic` validation.
2. Assert truncation behaviour at the `BODY_CAP_BYTES` boundary.
3. Assert that `base_url` userinfo and `api_key` query strings are stripped before serialization (security: this envelope ends up in logs).
4. Assert the `kind` discriminator is fixed (no other value validates).
