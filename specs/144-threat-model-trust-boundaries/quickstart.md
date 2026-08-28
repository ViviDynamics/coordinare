# Quickstart: Verifying the Threat Model and the Localhost Guard

**Feature**: 144-threat-model-trust-boundaries | **Date**: 2026-08-27

## 1. The guard, by test

```bash
.venv/bin/pytest tests/unit/test_144_threat_model_trust_boundaries.py -v
```

Covers the full decision table from
[contracts/localhost-guard.md](./contracts/localhost-guard.md), the permitted-set derivation
including IPv6 bracketing and port handling, the fail-closed property, and the startup warning.

## 2. The guard, by hand

With the daemon running on its defaults:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8090/api/force-poll
```

Expect **200**. No `Origin` header means a non-browser caller, which row 4 of the decision table
allows deliberately.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: https://evil.example' http://127.0.0.1:8090/api/force-poll
```

Expect **403**, with a body naming `Origin`. This is the cross-site request forgery case: a page
in the operator's browser trying to drive the dashboard.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8090/api/config/global
```

Expect **403**, with a body naming `Host`. Note this is a **GET**, and it is rejected. That is the
DNS-rebinding case, and it is the reason the host check covers reads. Before this feature, that
request succeeded.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/events
```

Expect **200**. The SSE stream must keep working (FR-017).

## 3. Prove the guard has teeth

A guard that never fires is indistinguishable from one that is not installed. Worth doing once
during review rather than trusting the green tick:

- Temporarily add a new mutating route to the dashboard without touching the guard, then confirm
  the fail-closed test rejects a foreign origin against it. This is FR-012, and it is the property
  most likely to quietly stop being true.
- Temporarily add `evil.example` to `trusted_dashboard_hosts` and confirm the `Host` rejection
  above becomes a 200. That proves the allowlist is actually consulted rather than decorative.

## 4. The warning

```bash
# Loopback: expect NO warning
COORDINARE_DASHBOARD_HOST=127.0.0.1 <start the daemon>

# Non-loopback: expect a warning naming the absence of authentication
COORDINARE_DASHBOARD_HOST=0.0.0.0 <start the daemon>
```

The second must say plainly that the dashboard has no authentication. The first must say nothing,
because a warning everyone sees on every start is a warning nobody reads (FR-021).

## 5. The documents

```bash
sed -n '/^## Architecture and trust boundaries/,/^## /p' SECURITY.md
```

Confirm spec 142's reserved section is **filled**, and that the surrounding file is unchanged
apart from it (FR-008).

Then read `docs/security/threat-model.md` as a self-hoster would and check the thing that matters
most: **every trust boundary states a residual risk**. A boundary with mitigations and no residual
risk is either wrong or is marketing, and a reader who later finds the gap stops trusting the
whole document.

Finally, confirm every test name the threat model cites actually exists (research D5). That check
is automated, but it is worth understanding why it is the only automated check on the prose: tests
assert behaviour, and the document cites the tests, so the claim and its evidence sit together
without a brittle assertion on wording.

## 6. What this feature does not do

| Not here | Owner |
|---|---|
| Dashboard authentication | spec 143 / [#197](https://github.com/ViviDynamics/coordinare/issues/197) |
| Removing internal infrastructure references | spec 145 / [#199](https://github.com/ViviDynamics/coordinare/issues/199) |
| Changing the `docker.sock` mount or performer sandboxing | documented here, not changed |
| Rate limiting, audit logging, CSRF tokens | none, deliberately out of scope |

The health endpoint's `0.0.0.0` default is **kept**, made configurable, and recorded as residual
risk. That is a decision (research D4), not an omission.
