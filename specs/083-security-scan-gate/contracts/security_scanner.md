# Contracts: Security Scan Gate (P1)

This feature has no HTTP/REST surface. The "contracts" are the internal Python interfaces the
implementation must satisfy. Each contract has a corresponding RED-first unit test (see
quickstart.md / tasks).

---

## Contract 1: `security_scanner.scan_diff`

**Module**: `src/coordinare/services/security_scanner.py` (new)

```python
def scan_diff(changed_files: list[str], repo_root: str | Path) -> list[Finding]:
    """Run semgrep + bandit over the changed files; return normalized findings.

    Finding = dict with keys: severity, category, description, file, line, routing
    (the spec-022 schema; see data-model.md).
    """
```

**Behavioral contract**
- MUST invoke semgrep (`semgrep --config auto --json`) and bandit (`bandit -f json -r ...`)
  scoped to `changed_files` under `repo_root`.
- MUST normalize both tools' output into the finding schema, defaulting `routing="implementer"`.
- MUST return `[]` for a clean scan (no tool findings).
- MUST be deterministic given fixed tool output (pure transform over subprocess results).
- MUST raise `ScannerError` (or a documented exception) on tool-missing / non-zero-unexpected /
  unparseable output, so the dispatch layer can apply fail-closed handling. (Tool "found
  issues" non-zero exit is NOT an error — it is normal.)
- MUST NOT log raw diff text or `auth_env` values; INFO logs limited to scan summary
  (counts/severities) (FR-011).

**Tests (RED first)** — `tests/unit/services/test_security_scanner.py`
- injection fixture → exactly one `critical`/`high` finding with correct file/line/category
- clean fixture → `[]`
- semgrep-only fixture (bandit empty) → semgrep findings normalized
- bandit-only fixture (semgrep empty) → bandit findings normalized
- malformed/garbage tool output → raises `ScannerError`
- tool-missing (binary not found) → raises `ScannerError`

---

## Contract 2: `github.get_pr_diff`

**Module**: `src/coordinare/services/github.py` (extend; sits beside `get_pr_files`:989)

```python
def get_pr_diff(pr_url: str) -> tuple[str, list[str]]:
    """Return (raw_unified_diff, changed_files) for the PR via gh/GitHub API."""
```

**Behavioral contract**
- MUST return the unified diff string and the list of changed file paths.
- MUST use the coordinare's existing GH auth (token already held).
- MUST raise on fetch failure (network/auth/not-found) so dispatch applies fail-closed.
- MUST NOT log the token or raw diff at INFO.

**Tests (RED first)** — in the github service test module
- mocked `gh pr diff` success → returns parsed (diff, files)
- fetch failure → raises (drives fail-closed path)

---

## Contract 3: Dispatch integration (run-once + card_context injection)

**Module**: `src/coordinare/graph/nodes/dispatch_performer.py` (modify ~:671 card_context build)

**Behavioral contract**
- WHEN `role == "security"`: fetch diff (`get_pr_diff`), call `scan_diff(...)`, stash result in
  `state["scanner_findings"]`, and add a `scanner_findings` block to `card_context`.
- ON scan/diff error: stash `[{severity: critical, category: scanner_unavailable,
  routing: halt, description: <safe>, file: "", line: 0}]` (fail-closed) and emit the
  observability marker.
- MUST scan exactly once (no scan in monitor). Non-security roles: no scan, no state key.

**Tests (RED first)** — dispatch node tests
- security role → `scan_diff` called once, `state["scanner_findings"]` populated, card_context
  has `scanner_findings` block
- non-security role → `scan_diff` NOT called
- scan raises → fail-closed synthetic finding stashed + marker emitted

---

## Contract 4: Monitor floor enforcement

**Module**: `src/coordinare/graph/nodes/monitor_performer.py` (modify ~:2196, the
`security_failed`/verdict-consumption block)

**Behavioral contract**
- BEFORE accepting `security_passed`: read `state["scanner_findings"]`. If any finding is
  `critical` or `high`, FORCE `security_failed` and merge the scanner findings into
  `relay_feedback` — regardless of the model's `passed` value.
- Medium/low-only findings → do NOT override.
- Model already `security_failed` → leave failed; still merge scanner findings into feedback.
- Synthetic `scanner_unavailable` (critical, routing=halt) → forces `security_failed`,
  feedback routes to halt/human attention, loud marker.
- MUST reuse the dispatch-stashed findings (no re-scan).

**Tests (RED first)** — `tests/unit/graph/nodes/test_monitor_performer.py`
- **082 regression**: scanner critical + model `passed:true` → overridden to `security_failed`
- scanner medium/low + model pass → NOT overridden
- scanner `scanner_unavailable` → fail-closed `security_failed`, routed to halt
- clean scanner + model pass → `security_passed` stands

---

## Contract 5: CWE persona (P2)

**Module**: `src/coordinare/services/persona_service.py` (modify security persona ~:326)

**Behavioral contract**
- Rendered `security` persona MUST instruct the model to: enumerate untrusted sources → trace
  to dangerous sinks → walk a fixed CWE list (injection, broken authz, hardcoded secrets,
  insecure deserialization, path traversal, SSRF) → emit the unchanged `{passed, findings[]}`
  JSON.
- Output contract (`{"passed": bool, "findings": [...]}`) UNCHANGED.

**Tests (RED first)** — `tests/unit/test_persona_service.py`
- rendered security persona string contains the taint→sink checklist structure (sources, sinks,
  fixed CWE list) AND still specifies the `{passed, findings[]}` JSON shape

---

## Contract 6: Config denylist (P3)

**Module**: `src/coordinare/config.py` (add load-time `model_validator`)

**Behavioral contract**
- Config load MUST raise a validation error if the resolved `security` role model is in the
  denylist {`qwen3.6:35b`, `qwq:32b`, `qwen2.5:14b-instruct`, `qwen2.5:32b`, `qwen3-coder:30b`}.
- Capable model → no error. reviewer/assessor on a denylisted model → no error (scope =
  `security` only).

**Tests (RED first)** — `tests/unit/test_config*.py`
- security → denylisted model → raises at load
- security → capable model → loads cleanly
- reviewer/assessor → denylisted model → loads cleanly (out of scope)
