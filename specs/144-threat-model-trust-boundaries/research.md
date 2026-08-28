# Phase 0 Research: Threat Model and Localhost Guard

**Feature**: 144-threat-model-trust-boundaries | **Date**: 2026-08-27

Six decisions. D4 and D5 changed the plan.

---

## D1: Guard placement, ordering, and how "mutating" is determined

**Decision**: A single `@app.middleware("http")` installed in `dashboard.py` **before** the
existing request logger at `:3896`, delegating to a function in the new
`src/coordinare/localhost_guard.py`. "Mutating" is determined by HTTP method:
`POST`, `PUT`, `PATCH`, `DELETE` are mutating; `GET`, `HEAD`, `OPTIONS` are not.

**Rationale**: Middleware is the only mechanism that satisfies FR-012. With 24 mutating routes
today, a per-route decorator is one forgotten annotation away from a hole, and the failure is
silent: the route works, it is simply unguarded. Middleware inverts that, so a new route is
guarded unless someone deliberately exempts it.

Method-based classification rather than a path allowlist for the same reason. A path list would
need maintaining alongside the routes, and would drift.

**Ordering**: the guard is installed **before** the logger in source order. Verified empirically
rather than assumed, because the intuition runs backwards: FastAPI's `@app.middleware("http")`
makes the **last-added** middleware outermost. Installing the guard first therefore makes the
logger the outer layer, which is what we want on both counts. Every request including a rejected
one still appears in the request log, and the guard still refuses before any route handler runs.

(An earlier draft of this decision said "the guard runs first". That is false: the *logger* runs
first. The property that matters is that the guard runs before any **handler**, which it does.)

**Alternatives considered**: Per-route dependency injection via `Depends` (rejected: opt-in, so it
fails FR-012). A Starlette `TrustedHostMiddleware` (rejected: it handles `Host` only, not
`Origin`, and cannot express the method-dependent rule). Path-prefix matching on `/api/`
(rejected: couples the guard to a naming convention rather than to what actually mutates state).

---

## D2: Deriving the permitted host and origin sets

**Decision**: Compute both sets once at application construction from configuration, never per
request. Classify hosts by parsing the address with the standard library's `ipaddress` module and
testing `is_loopback`, rather than string-matching known spellings.

**Permitted hosts**: any address that parses as a loopback IP (covering `127.0.0.0/8` in full, not
just `127.0.0.1`, and `::1`), the literal name `localhost`, the configured `dashboard_host`, and
any hostname the operator has explicitly added (D3). The port is compared against
`dashboard_port` when a port is present, satisfying FR-013.

**Permitted origins**: the same host set, with scheme `http` or `https`, and the configured port.

**Two parsing details that will otherwise cause bugs**:
- The `Host` header for IPv6 is **bracketed**: `[::1]:8090`. The brackets must be stripped before
  `ipaddress` will parse it, and a naive `rsplit(":", 1)` to remove the port corrupts a bare IPv6
  address. Split on the last colon only when the value has no brackets, or after the closing
  bracket when it does.
- A `Host` may legitimately carry no port when the default port for the scheme is used, so an
  absent port must not be treated as a mismatch.

**Rationale**: `ipaddress.is_loopback` is correct by construction where an enumerated list of
spellings is a list someone will forget to extend. It also correctly accepts `127.0.0.2`, which is
loopback and which a hand-written list would reject.

**Alternatives considered**: Comparing against a hardcoded tuple of three spellings (rejected:
misses `127.0.0.x` and is exactly the kind of list that rots). Resolving hostnames via DNS
(rejected: introduces per-request I/O and a DNS-dependent security decision, which is the attack
being defended against).

---

## D3: Letting an operator front the dashboard with a proxy

**Decision**: A new configuration field `trusted_dashboard_hosts: list[str]`, defaulting empty,
mirroring the existing `trusted_bot_reviewers: list[str]` pattern at `config.py:819`. A hostname
present there is permitted for both `Host` and `Origin`. Setting it is documented in the threat
model as widening exposure.

**Rationale**: FR-015 requires the mechanism be explicit and never implicit. An empty default means
the safe configuration is the one an operator gets by doing nothing, and reaching the wider one
requires naming the exact hostname, which is a moment where they have to think about it. Mirroring
an existing config idiom keeps the surface familiar.

**Deliberately rejected**: honouring `X-Forwarded-Host`. It is attacker-controlled unless a proxy
is known to overwrite it, and trusting it by default would silently undo the whole guard. An
operator who terminates at a proxy names the hostname instead.

**Alternatives considered**: A boolean "disable the guard" escape hatch (rejected: an all-or-
nothing switch invites being flipped for an unrelated problem, and then never flipped back). A
wildcard pattern (rejected: unnecessary for the actual use case, and a wildcard in a security
allowlist is usually the beginning of a story).

---

## D4: The health bind default (**changed the plan**)

**Finding**: `src/coordinare/__main__.py:1034` hardcodes `host="0.0.0.0"` for the health app, with
`:949` checking port availability on the same. The dashboard uses `config.dashboard_host`
(default `127.0.0.1`). The obvious "fix" is to make health match the dashboard and default to
loopback. **That would be a silent breaking change.** `README.md:119` documents the health
endpoint as "JSON health check for load balancers", which is by definition reached from another
host. A daemon running in a container that binds loopback is unreachable from the host entirely.

Complicating it in the other direction: `docker-compose.yml` publishes no ports and defines no
`HEALTHCHECK`, so nothing in the repository actually depends on the current binding.

**Decision**: Add `health_check_host` to configuration, **defaulting to `0.0.0.0`, preserving
today's behaviour**. Do not change the effective default. Instead:

1. Make it configurable, so an operator who wants loopback can have it (FR-019).
2. Document the difference from the dashboard's default and why it exists (FR-022): the health
   endpoints are liveness signals designed to be polled by an orchestrator, whereas the dashboard
   is a control plane.
3. Document precisely what the port discloses (FR-005), which is the honest mitigation, since
   `/metrics` reveals card counts, model identifiers, and error rates to anyone who can reach it.
4. Warn when health is non-loopback **and** metrics are exposed, so the disclosure is a decision
   rather than a surprise.

**Rationale**: A security feature that silently breaks a documented deployment teaches operators
to distrust upgrades, which costs more security than this default gains. The endpoints are
read-only; the exposure is disclosure, not control. Making it visible and configurable addresses
the real problem, and an operator who wants the tighter posture now has one line to change.

**Recorded as residual risk** in the threat model rather than resolved. That is the honest
description of what this decision does.

**Alternatives considered**: Defaulting health to loopback (rejected: breaks a documented use
silently). Splitting `/metrics` onto its own port (rejected: real improvement, but new surface
area beyond this spec's remit, and it would need its own configuration and migration story).
Requiring authentication on `/metrics` (rejected: spec 143 owns authentication).

---

## D5: Keeping the threat model true (**changed the plan**)

**Decision**: Tests assert **behaviour**, never document prose. The threat model then cites the
test names that hold each checkable claim up, in a short "How to verify these claims" section.

**Rationale**: The tension recorded in the checklist is real, and both naive options are bad.
Asserting document content produces a test that fails when someone improves a sentence, which
trains people to weaken the test. Asserting only behaviour lets the document drift into
describing a system that no longer exists, which is worse than having no document because it is
confidently wrong.

Citing test names from the prose resolves it. The claim and its evidence sit together, a reader
can run the named test, and an editor who wants to change a claim can see the test that will
contradict them. The only automated check is the cheap one: that every test name cited in the
document actually exists, so a rename cannot leave a dangling citation.

**What this covers** (FR-026): the guard's behaviour, the warning's trigger condition, and the
bind defaults. It deliberately does not attempt to test claims about prompt injection or
`docker.sock`, which are architectural facts rather than checkable properties.

**Alternatives considered**: Asserting key phrases appear (rejected: brittle in exactly the way
spec 142's file-scoped wording guard avoided by being total-within-scope rather than
phrase-matching). A documentation freshness date checked against git (rejected: a date proves
someone touched the file, not that it is true).

---

## D6: Where the guard lives

**Decision**: A new module, `src/coordinare/localhost_guard.py`, exposing the permitted-set
derivation and the middleware factory. `dashboard.py` imports and installs it.

**Rationale**: Three reasons, in order of weight. The threat model must point at the mitigation,
and pointing at a named module is materially better than pointing into a 5,000-line file. The
permitted-set derivation is the part most likely to be subtly wrong (D2's IPv6 bracket and port
handling) and deserves direct unit tests that do not require constructing the whole dashboard
app. And the health application is a plausible future consumer, which an inline closure would
force to duplicate.

**Alternatives considered**: Inline in `dashboard.py` next to the logger (rejected: for the
reasons above, though it would be marginally less code). A package under `src/coordinare/security/`
(rejected: premature structure for one module; promote it if a second arrives).


---

## D7: The webhook route must be exempt (**found during implementation**)

**Finding**: `dashboard.py` contains 24 mutating decorators but the constructed application has
only 23 mutating routes. The 24th is `register_webhook_route` (`dashboard.py:5431`), which
`__main__` calls *after* construction, and only when `config.webhooks.enabled` and a secret are
set.

Because the guard is middleware, it covers routes registered after construction too. That is
normally exactly the point. Here it is a production outage waiting to happen: **GitHub calls the
webhook endpoint from the internet**, so its `Host` is never local, every delivery would be
refused with 403, and enabling webhooks would silently stop working.

**Decision**: `install_localhost_guard` takes `exempt_paths`, and `__main__` passes the configured
webhook path when webhooks are enabled.

**Why this is sound rather than a hole**: the webhook route already authenticates itself, with an
HMAC signature over the body verified against a shared secret (`verify_github_signature`,
rejecting with 401). It is not an unauthenticated route being waved through. It is an
externally-authenticated route being exempted from a *locality* check that does not apply to it.

**Two constraints on the mechanism**, both tested: exemptions are exact path matches rather than
prefixes, so one entry cannot widen to a subtree; and the test asserts an exempt route reached
with a foreign host returns **401 from its own authentication**, not 200. Exempt from the guard is
not the same as unauthenticated, and the test enforces that distinction.

**Why it was missed at planning**: the route count came from grepping decorators in the file,
which conflated "decorators present in source" with "routes the application actually constructs".
Enumerating the live route table is what surfaced it. The general lesson is worth keeping: counting
source constructs is not the same as observing runtime state, and only the latter finds a route
registered from somewhere else.


---

## D8: The port comparison is removed (**found by CI hanging**)

**Finding**: D2 specified that a `Host` carrying a port must match `dashboard_port`. That shipped,
and the `E2E Browser Tests` job hung for 35 minutes before being cancelled. Eight of nine jobs were
green; only the one that drives a real browser was stuck.

`tests/e2e/conftest.py` binds a **random free port** via `_free_port()`, so Playwright navigated to
`http://127.0.0.1:<random>`, the browser sent that as `Host`, the guard compared the port against
8090, refused with 403, and every page load failed. Playwright then waited for selectors that would
never appear.

**Decision**: the port is no longer used to accept or reject. It is retained on `PermittedOrigins`
only so the rejection message can name the expected port as a diagnostic.

**Why the comparison was wrong on its own terms, not merely inconvenient**: a `Host` port is set by
the browser from the URL it connected to, and a request cannot arrive on a port the server is not
listening on. A mismatched port can therefore only come from a non-browser client, which this guard
never authenticated. DNS rebinding is caught by the hostname check and is untouched. The comparison
bought nothing.

**Verified both directions** rather than assumed. With the comparison restored: `GET / → 403` and
`test_page_loads_with_navbar` fails in 4.5 seconds. Without it: 98 end-to-end tests pass in 66
seconds. Foreign hosts (`evil.example`, `192.168.1.50:8090`) are still refused either way, which is
the property that actually matters.

**The general lesson**: the e2e suite was the first victim, not the only possible one. Any embedding
of the dashboard on a port other than the configured one would have been silently dead. A security
check that fails closed on legitimate traffic is a bug even when the traffic is only a test, because
the test was standing in for a real deployment shape.
