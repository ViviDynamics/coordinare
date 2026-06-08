# Phase 1 Data Model: Security Scan Gate

No new persisted entities and **no protocol schema change** (FR-012). This feature reuses the
existing spec-022 finding schema and the `security_passed`/`security_failed` status enum, and
adds one transient graph-state key.

## Entity: Finding (reused — spec-022 schema)

A normalized static-analysis or model result. Already carried by
`ProtocolResponse.findings: list[dict]` (`src/coordinare/protocol.py:61`). Scanner findings use
the **same** dict shape so they flow through `card_context` and `relay_feedback` unchanged.

| Field | Type | Notes |
|-------|------|-------|
| `severity` | str | one of `critical` / `high` / `medium` / `low`. **Floor triggers on `critical` or `high`.** |
| `category` | str | CWE-ish label, e.g. `injection`, `hardcoded_secret`, `path_traversal`, `ssrf`, `scanner_unavailable`. |
| `description` | str | human-readable finding text. MUST NOT embed raw diff/secret values. |
| `file` | str | path of the offending file (from tool location). Empty/`""` for `scanner_unavailable`. |
| `line` | int | line number (from tool location). `0` when not applicable. |
| `routing` | str | role to fix it. Scanner default `implementer` (FR-002); `scanner_unavailable` uses `halt` (FR-008). |

### Validation / invariants
- `severity ∈ {critical, high, medium, low}` — normalizer guarantees this; unknown tool
  severities map conservatively (toward `high` for dangerous CWEs, else `medium`).
- Scanner findings default `routing = "implementer"`; the synthetic fail-closed finding uses
  `routing = "halt"`.
- `description` is summary-safe — never contains `auth_env`-resolved values or raw diff text
  (FR-011).

### Severity mapping (concrete)

semgrep (`results[].extra.severity`):
| semgrep | → finding `severity` |
|---------|----------------------|
| `ERROR` (or rule tagged critical/CWE-injection class) | `critical` |
| `WARNING` + dangerous CWE | `high` |
| `WARNING` (other) | `medium` |
| `INFO` | `low` |

bandit (`results[].issue_severity` × `issue_confidence`):
| severity / confidence | → finding `severity` |
|-----------------------|----------------------|
| HIGH / HIGH | `critical` |
| HIGH / MEDIUM, MEDIUM / HIGH | `high` |
| MEDIUM / * | `medium` |
| LOW / * | `low` |

## Graph state key: `scanner_findings` (new, transient)

| Aspect | Value |
|--------|-------|
| Key | `state["scanner_findings"]` |
| Type | `list[Finding]` (list of the dicts above) |
| Lifecycle | Written once at dispatch (`dispatch_performer.py`) when `role == "security"`; read at dispatch (card_context injection) and at verdict time (`monitor_performer.py` floor). Transient — mirrors `relay_feedback`, never persisted. |
| Producer | `security_scanner.scan_diff(...)` via dispatch integration, OR the synthetic fail-closed finding on scanner/diff error. |
| Consumers | (1) dispatch: injected into `card_context` as a `scanner_findings` block (advisory ceiling). (2) monitor: floor enforcement — if any entry is `critical`/`high`, force `security_failed` and merge into `relay_feedback`. |

### State transition (verdict floor)
```
model verdict = security_passed
  └─ scanner_findings has critical/high?
        ├─ yes → OVERRIDE to security_failed; merge scanner findings into relay_feedback
        └─ no  → verdict stands (security_passed)
model verdict = security_failed → unchanged (merge scanner findings into relay_feedback)
scanner/diff error at dispatch → scanner_findings = [synthetic critical scanner_unavailable, routing=halt]
  └─ monitor floor forces security_failed (fail-closed)
```

## Status enum (reused)

`security_passed` / `security_failed` already exist (`src/coordinare/protocol.py:20-21`). No new
status values. The floor only ever *forces* an existing value (`security_failed`).
