# Implementation Plan: Security Performer

**Branch**: `022-security-performer` | **Date**: 2026-03-18 | **Spec**: [spec.md](./spec.md)

## Summary

Extend the existing performer codebase to support the `security` role. The security performer analyses the feature branch diff for vulnerabilities (OWASP Top 10, secrets, CVEs, insecure defaults), categorises findings by severity, posts advisory comments to the PR for medium/low findings, and returns `security_passed` (no blocking findings) or `security_failed` (one or more critical/high findings with routing metadata). On `security_failed`, the coordinare routes findings to the implementer or architect. Max fix cycles enforced via `SECURITY_MAX_CYCLES`.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `httpx` (existing), `pydantic>=2.9` (existing) — **no new dependencies**
**Storage**: None
**Testing**: pytest (existing in `agent/performer/tests/`)
**Target Platform**: Linux container (base performer image — static analysis only, no runtime execution)
**Performance Goals**: Security scan subject to `AGENT_TIMEOUT`; finding routing decided within one status poll
**Constraints**: `SECURITY_MAX_CYCLES` env var (default: 3); advisory findings never block; false positive suppression is out of scope
**Scale/Scope**: ~4 modified files in performer package, ~15 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | `security_passed`/`security_failed` are clean terminal states; finding structure is explicit (severity, category, routing) |
| II. Testing Discipline | PASS | Tests required for pass path, fail-with-routing path, advisory-only path, max-cycle limit |
| III. User Experience | N/A | Internal performer |
| IV. Performance by Design | PASS | One extra GitHub API call for advisory PR comments; negligible overhead |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers |

## Project Structure

### Source Code (files changed)

```text
agent/performer/src/performer/
├── main.py          # MODIFIED — security_passed/security_failed states; finding routing; SECURITY_MAX_CYCLES
├── models.py        # MODIFIED — security_passed, security_failed statuses; SecurityFinding model; check_attempt
├── github.py        # MODIFIED — add post_pr_comment(owner, repo, pr_number, body, token) for advisory comments
└── config.py        # MODIFIED — add SECURITY_MAX_CYCLES setting

agent/performer/tests/unit/
├── test_main.py     # MODIFIED — security performer path tests
└── test_github.py   # MODIFIED — advisory PR comment helper tests
```

## Detailed Implementation Plan

### Step 1 — Security finding model (`models.py`)

```python
class SecurityFinding(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    category: str            # e.g. "secret_leakage", "sql_injection", "missing_auth"
    description: str
    file: str | None = None
    line: int | None = None
    routing: Literal["implementer", "architect"]  # determines relay target in coordinare

class Performance(BaseModel):
    ...
    security_findings: list[SecurityFinding] = []
    security_cycle: int = 0
```

Add `"security_passed"` and `"security_failed"` to `PerformerStatus`.

**`config.py`**: Add `SECURITY_MAX_CYCLES: int = 3`.

### Step 2 — Advisory comment helper (`github.py`)

```python
async def post_pr_comment(owner: str, repo: str, pr_number: int, body: str, token: str) -> dict:
    """POST /repos/{owner}/{repo}/issues/{pr_number}/comments — general PR comment."""
```

Used for medium/low findings labelled `[Advisory - Security]`.

### Step 3 — Security logic in `handle_status()` (`main.py`)

When `perf.role == "security"` and backend returns `done`:

```python
findings = parse_security_findings(backend_status.output)  # list[SecurityFinding]

# Post advisory comments for medium/low findings
advisory = [f for f in findings if f.severity in ("medium", "low")]
for finding in advisory:
    await github.post_pr_comment(owner, repo, pr_number,
        body=f"[Advisory - Security] **{finding.category}** ({finding.severity})\n\n{finding.description}",
        token=token)

# Check for blocking findings
blocking = [f for f in findings if f.severity in ("critical", "high")]
if not blocking:
    perf.state = "security_passed"
    return PerformerResponse(status="security_passed", session_id=perf.session_id)

perf.security_cycle += 1
if perf.security_cycle >= settings.SECURITY_MAX_CYCLES:
    perf.state = "blocked"
    perf.open_questions = [f"Security: {len(blocking)} blocking finding(s) after {perf.security_cycle} fix attempt(s)"]
    return PerformerResponse(status="blocked", ...)

perf.security_findings = blocking
perf.state = "security_failed"
return PerformerResponse(
    status="security_failed",
    session_id=perf.session_id,
    findings=[f.model_dump() for f in blocking],
)
```

### Step 4 — Coordinare-side routing (tracked here, implemented in 019/coordinare)

The coordinare's `monitor_performer` node reads the `findings` list from a `security_failed` response, partitions by `routing` field, and relays each partition to the appropriate performer via `relay_feedback`. The security performer does not directly invoke the implementer or architect — it only declares the routing in the response.

## Complexity Tracking

No constitution violations.
