# Phase 1 Data Model: Threat Model and Localhost Guard

**Feature**: 144-threat-model-trust-boundaries | **Date**: 2026-08-27

**No persistence.** No coordinare state, no `state_store.py` field, no schema bump. The shapes
below live in process memory for the lifetime of the application, plus two new configuration
fields.

---

## PermittedOrigins

Computed once when the dashboard application is constructed, then read on every request.

| Field | Type | Notes |
|---|---|---|
| `hosts` | `frozenset[str]` | Lower-cased literal names permitted outright, e.g. `localhost`, the configured `dashboard_host`, and each `trusted_dashboard_hosts` entry. |
| `port` | `int` | The configured `dashboard_port`. A header carrying a different port is rejected; one carrying no port is accepted. |
| `allow_loopback_ips` | `bool` | Always true in practice. Present so the derivation is explicit rather than implied by a magic branch. |

**Validation rules**:
- Built at construction, never mutated afterwards. A guard whose permitted set can change at
  runtime is a guard with a race in it.
- Entries are lower-cased on the way in, so the per-request comparison needs no normalisation
  beyond lower-casing the incoming header.
- An empty `hosts` set is impossible: `localhost` and the configured bind host are always present.

---

## RequestVerdict

The result of applying the decision table to one request. Not stored; produced and consumed
within the middleware.

| Field | Type | Notes |
|---|---|---|
| `allowed` | `bool` | |
| `rejected_header` | `"Host" \| "Origin" \| None` | Which check failed. `None` when allowed. |
| `reason` | `str \| None` | Human-readable, safe to log and to return. Names what was expected. |

**Validation rule**: when `allowed` is false, both `rejected_header` and `reason` are required
and non-empty (FR-016). A rejection with no reason is the failure mode this feature exists to
avoid producing in operators.

---

## Configuration additions

Two new fields on the existing config model. Both are additive with defaults, so existing
configuration files load unchanged.

| Field | Type | Default | Why this default |
|---|---|---|---|
| `health_check_host` | `str` | `"0.0.0.0"` | **Preserves current behaviour deliberately** (research D4). `README.md:119` documents the health endpoint as a load-balancer target, and a daemon in a container that binds loopback is unreachable from the host. Changing it would be a silent breaking change. |
| `trusted_dashboard_hosts` | `list[str]` | `[]` | Empty means the safe posture is what an operator gets by doing nothing. Widening it requires naming an exact hostname, which is a moment to think (FR-015). Mirrors the existing `trusted_bot_reviewers` idiom at `config.py:819`. |

**Note on `health_check_host`**: leaving the default at `0.0.0.0` is not an oversight and is
recorded as residual risk in the threat model rather than presented as a mitigation. The health
endpoints are read-only, so the exposure is disclosure rather than control, and `/metrics` is the
part that discloses.

---

## TrustBoundary

A documentation entity, not code. Each of the four gets the same treatment in the threat model so
a reader can compare them.

| Field | Notes |
|---|---|
| `name` | e.g. "operator host to daemon container" |
| `what_crosses` | The data or control that moves across it |
| `trusted_assumption` | What is assumed about the other side |
| `mitigations` | What actually constrains it today, with the code that does so |
| `residual_risk` | What remains, stated plainly. **Required and non-empty.** |

**Validation rule**: `residual_risk` may not be empty for any boundary. A boundary described with
mitigations and no residual risk is either wrong or is marketing, and either way a reader who
later finds the gap stops trusting the whole document (FR-003).

---

## TokenPermissionRow

| Field | Notes |
|---|---|
| `feature` | The coordinare capability an operator wants |
| `permission` | The fine-grained GitHub permission it needs |
| `access` | Read or write |
| `required_for_minimal_run` | Whether a first run needs it, or only an optional feature (FR-024) |
