# Feature Specification: Threat Model, Trust Boundaries, and Cheap Hardening

**Feature Branch**: `144-threat-model-trust-boundaries`
**Created**: 2026-08-27
**Status**: Draft
**Issue**: [#198](https://github.com/ViviDynamics/coordinare/issues/198) (launch-blocking)
**Input**: Write coordinare's threat model and trust-boundary documentation, and land the cheap hardening fixes from the 2026-08-07 pre-launch audit that need no new authentication machinery.

## Clarifications

### Session 2026-08-27

- Q: How wide should the localhost guard be? → A: **Origin checked on the mutating routes, Host checked on every route including reads.** They defend different attacks: the Origin check closes browser CSRF, while the Host check closes DNS rebinding, and rebinding reaches read endpoints that the Origin check cannot protect because browsers send no `Origin` on a same-origin GET. A mutating-only guard would leave `GET /api/config/global` and similar exfiltratable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A self-hoster can see what they are trusting before they expose it (Priority: P1)

Someone evaluating coordinare for their own infrastructure needs to know, before they run it
anywhere but a laptop, what it executes, what credentials it holds, what hostile input reaches
it, and which of those risks the project has actually mitigated rather than merely noted. They
should be able to reach that understanding from one document, and they should be able to trust
it because it admits what is still exposed.

**Why this priority**: This is the launch gate. Coordinare executes AI-generated code, holds a
repository-write credential, ingests attacker-authored text into model prompts, and mounts
`docker.sock`. Publishing that without a written security posture invites both real incidents
and reputational ones. A reader who cannot find the risks will assume there are none.

**Independent Test**: A reader who has never seen coordinare opens `docs/security/threat-model.md`
and can name the four trust boundaries, state what prompt injection can and cannot achieve
against a coordinare deployment, explain why the dashboard has no login, and identify which
GitHub token permissions their intended feature set requires. They can also point to at least
one place where the document says a risk is *not* mitigated.

**Acceptance Scenarios**:

1. **Given** the threat model, **When** a reader looks for trust boundaries, **Then** all four
   are described: operator host to daemon container, daemon to performer containers, performers
   to public GitHub content, and coordinare to model endpoints.
2. **Given** the prompt-injection section, **When** a reader finishes it, **Then** it names the
   existing mitigations and **also** states the residual risk in plain terms, rather than
   listing defences and stopping.
3. **Given** the dashboard section, **When** a reader asks why there is no login, **Then** the
   answer is stated as a deliberate decision with its boundary (loopback only, never exposed to
   an untrusted network) and its future path (spec 143).
4. **Given** the README, **When** a reader skims it, **Then** the threat model is linked
   prominently enough to be seen before deployment, not buried.
5. **Given** `SECURITY.md`'s reserved architecture section, **When** this feature completes,
   **Then** that section is filled in place, with the surrounding file unrestructured.

---

### User Story 2 - A hostile web page cannot drive the operator's dashboard (Priority: P1)

An operator has the dashboard open on loopback and, in another tab, visits a page that is
hostile or merely compromised. That page must not be able to rewrite coordinare's configuration,
delete a symphony, cancel work, or read the operator's configuration back out.

**Why this priority**: Equal to the documentation, because it is the one exposure the audit
found that is exploitable without the operator doing anything unusual. The dashboard constructs
**23 mutating routes** including `DELETE /api/symphonies/{name}` and `PUT /api/config/global`, and
it has no login by design, so a browser that can reach `127.0.0.1` can currently drive all of them.
(A 24th mutating decorator exists in the module but belongs to the webhook route, which is
registered separately and is deliberately exempt: see FR-027.)

**Independent Test**: A request carrying a foreign `Origin` to any mutating route is rejected; a
request carrying a foreign `Host` to any route, read or mutating, is rejected; a request from the
dashboard's own page succeeds; a request with no `Origin` at all (a script, not a browser)
succeeds.

**Acceptance Scenarios**:

1. **Given** a mutating request whose `Origin` is not a permitted local origin, **When** it
   arrives, **Then** it is rejected before the route handler runs and nothing is mutated.
2. **Given** a mutating request with **no** `Origin` header, **When** it arrives, **Then** it is
   allowed, because a browser always sends `Origin` on non-GET requests and its absence means the
   caller is not a browser, so no cross-site request forgery is possible.
3. **Given** any request, read or mutating, whose `Host` is not a permitted local host, **When**
   it arrives, **Then** it is rejected. This covers reads because a browser sends no `Origin` on
   a same-origin GET, so DNS rebinding against a read endpoint is invisible to the `Origin` check.
4. **Given** a newly added mutating route, **When** a developer adds it without doing anything
   special, **Then** it is guarded automatically. The guard must be fail-closed by default rather
   than opt-in per route.
5. **Given** the server-sent events stream and the dashboard's own page loads, **When** the guard
   is active, **Then** neither breaks.
6. **Given** a rejection, **When** the operator investigates, **Then** the response and the log
   say which header was rejected and why, so a legitimate configuration problem is not mistaken
   for a bug.

---

### User Story 3 - An operator who exposes coordinare is told plainly (Priority: P2)

An operator who changes the dashboard bind address away from loopback, perhaps to reach it from
another machine, should be told in plain language at startup that they have just put an
unauthenticated control plane on a network. An operator should also not discover by accident
that a second port was already listening on every interface.

**Why this priority**: Below the guard because it depends on the operator taking an action,
whereas the CSRF exposure needs nothing from them. Still important, because the current state is
inconsistent in a way nobody would predict: the dashboard defaults to loopback while the health
server binds every interface unconditionally.

**Independent Test**: Starting with a non-loopback dashboard bind emits a warning that names the
lack of authentication. The health server's bind address is configurable, defaults consistently
with the dashboard's posture, and its exposure is documented.

**Acceptance Scenarios**:

1. **Given** a non-loopback dashboard bind, **When** the daemon starts, **Then** a warning states
   that the dashboard has no authentication and should not be reachable from an untrusted
   network.
2. **Given** a loopback dashboard bind, **When** the daemon starts, **Then** no such warning is
   emitted, so the warning keeps its meaning.
3. **Given** the health server, **When** an operator wants it on loopback, **Then** its bind
   address is configurable rather than hardcoded.
4. **Given** the health server's default, **When** an operator has not configured anything,
   **Then** the default is documented, and any remaining difference from the dashboard's default
   is deliberate and explained rather than accidental.
5. **Given** the health endpoints, **When** the threat model describes them, **Then** it states
   what they disclose to anyone who can reach the port.

---

### User Story 4 - An operator can grant coordinare the least privilege that works (Priority: P2)

An operator setting up coordinare has to create a GitHub token. They should be able to grant
exactly the permissions their intended feature set needs, and no more, without discovering the
requirement by trial and error when a run fails halfway.

**Why this priority**: The token is the most valuable thing coordinare holds, and the current
absence of guidance pushes operators toward over-granting, which is the failure mode that makes
every other risk worse.

**Independent Test**: A reader can look up a feature they intend to use and find the specific
fine-grained permission it requires, then create a token from that list alone.

**Acceptance Scenarios**:

1. **Given** the token matrix, **When** an operator looks up a feature, **Then** the required
   fine-grained permission and access level are stated.
2. **Given** the matrix, **When** an operator wants a minimal deployment, **Then** they can tell
   which permissions are required for a first run versus which are needed only by optional
   features.
3. **Given** the matrix, **When** an operator asks about the future, **Then** the GitHub App path
   is noted as the direction of travel.

---

### Edge Cases

- **A request with no `Origin` must be allowed.** Command-line tools, scripts, monitoring probes,
  and the project's own test client all send none. Rejecting them would break legitimate
  automation for no security gain, since cross-site request forgery requires a browser and
  browsers always send `Origin` on non-GET requests.
- **The project's own tests currently send `Host: testserver`.** Six existing test files drive
  these routes through a default test client, which sends that host and no origin. They must be
  updated to present a local host, so that they exercise the guard rather than bypass it. Adding
  `testserver` to the permitted set instead would be a permanent hole in production, since the
  header is attacker-controlled.
- **A same-origin GET carries no `Origin`.** This is why the host check must cover reads. Relying
  on the origin check alone would leave configuration readable through DNS rebinding.
- **`Host` includes the port, and the port is configurable.** The permitted set must be derived
  from configuration rather than hardcoded, or an operator who changes the port locks themselves
  out.
- **IPv6 loopback and the literal `localhost` are both legitimate.** All spellings of loopback
  must be accepted, or the guard fails for ordinary local use.
- **A reverse proxy in front of the dashboard rewrites `Host`.** An operator deliberately
  fronting coordinare needs a way to permit their own hostname, or the guard makes a legitimate
  deployment impossible. Whatever mechanism allows that must be explicit, because it also widens
  the exposure.
- **The warning must not fire on the default configuration.** A warning that everyone sees on
  every start is a warning nobody reads.
- **The threat model can become false.** It describes mitigations that live in code, so it needs
  to be checkable rather than merely written, or it will drift into a document that describes a
  system that no longer exists.

## Requirements *(mandatory)*

### Functional Requirements

**Threat model document**

- **FR-001**: The repository MUST contain `docs/security/threat-model.md` describing all four
  trust boundaries: operator host to daemon container, daemon to performer containers, performers
  to public GitHub content, and coordinare to model endpoints.
- **FR-002**: The document MUST state that mounting `docker.sock` into the daemon grants
  root-equivalent access to the host, and MUST present this as an explicit trust decision rather
  than omitting it.
- **FR-003**: The document MUST contain a prompt-injection section that names the existing
  mitigations (binary reviewer verdicts, humans-only pull request approval, spec-131 agent-config
  commit blocking, spec-083 security scan gate, branch protection expectations, and spec-142's
  closed-contribution model) **and** states the residual risk in plain terms. A section that lists
  only defences does not satisfy this requirement.
- **FR-004**: The document MUST explain the unauthenticated dashboard as a deliberate decision,
  state its boundary (loopback only, never an untrusted network), and name spec 143 as the future
  authentication path.
- **FR-005**: The document MUST describe what the health endpoints disclose to anyone able to
  reach their port.
- **FR-006**: The document MUST state that performer output is untrusted, and describe what the
  security scan gate does and does not catch.
- **FR-007**: The README MUST link the threat model prominently enough that a reader encounters
  it before deploying, framed as something to read before exposing coordinare to a network.
- **FR-008**: `SECURITY.md`'s reserved architecture and trust-boundary section MUST be filled in
  place. The surrounding file MUST NOT be restructured, since spec 142 shaped it specifically to
  receive this content.

**Localhost guard**

- **FR-009**: Every mutating request whose `Origin` header is present and is not a permitted
  local origin MUST be rejected before its route handler executes.
- **FR-010**: A request with no `Origin` header MUST be allowed. Absence indicates a non-browser
  caller, and cross-site request forgery requires a browser.
- **FR-011**: Every request, read or mutating, whose `Host` header is not a permitted local host
  MUST be rejected. This is the defence against DNS rebinding, and it must cover reads because a
  browser sends no `Origin` on a same-origin GET.
- **FR-012**: The guard MUST apply automatically to routes added in future, without those routes
  opting in. A mechanism that requires per-route annotation does not satisfy this requirement,
  because the 23 existing mutating routes demonstrate the scale at which one would be forgotten.
- **FR-013**: The permitted host and origin sets MUST be derived from the running configuration,
  so that changing the bind address does not lock the operator out. The **port MUST NOT** be used
  to accept or reject a request. (Revised 2026-08-28, after the original version broke CI.)

  The port bought no security: a `Host` port is set by the browser from the URL it connected to,
  and a request cannot arrive on a port the server is not listening on, so a mismatched port can
  only come from a non-browser client, which this guard never authenticated. DNS rebinding is
  caught by the hostname check, which is unaffected.

  It also broke real deployments. Any application served on a port other than the configured one
  refused every request. The end-to-end browser suite binds a random free port, so every page load
  returned 403 and the CI job hung for 35 minutes. That was the first victim, not the only possible
  one.
- **FR-014**: All spellings of loopback MUST be permitted, including IPv4 loopback, IPv6
  loopback, and the literal name.
- **FR-015**: An operator deliberately fronting the dashboard with a proxy or a hostname MUST
  have an explicit, documented way to permit that hostname. It MUST NOT be permitted implicitly.
- **FR-016**: A rejection MUST state which header was rejected, in both the response and the log,
  so a misconfiguration is diagnosable rather than presenting as an unexplained failure.
- **FR-017**: The guard MUST NOT break the server-sent events stream, the dashboard's own page
  load, or its same-origin requests.
- **FR-018**: Existing tests that drive these routes MUST be updated to present a permitted local
  host, so they exercise the guard. The test-client default host MUST NOT be added to the
  permitted set, because that would be a permanent production hole.
- **FR-027**: A route that is **designed** to be called from outside, and that authenticates
  itself, MUST be exemptable from the guard by exact path. The GitHub webhook endpoint is the one
  such route today: GitHub calls it from the internet, and it verifies an HMAC signature over the
  body. Guarding it would break webhooks outright. An exemption MUST be an exact path match rather
  than a prefix, and anything exempted MUST carry its own authentication, since an exemption
  without authentication is simply an unguarded route. (Added 2026-08-27, discovered during
  implementation.)

**Bind consistency and warning**

- **FR-019**: The health server's bind address MUST be configurable rather than hardcoded.
- **FR-020**: The daemon MUST emit a startup warning when the dashboard is bound to a
  non-loopback address, stating plainly that the dashboard is unauthenticated and should not be
  reachable from an untrusted network.
- **FR-021**: That warning MUST NOT be emitted for a loopback bind, so that it retains meaning.
- **FR-022**: Any remaining difference between the health server's default bind and the
  dashboard's default bind MUST be deliberate and documented, not incidental.

**Token guidance**

- **FR-023**: A token-permission matrix MUST be documented, mapping each feature to the
  fine-grained permission and access level it requires.
- **FR-024**: The matrix MUST distinguish permissions required for a minimal first run from those
  needed only by optional features.
- **FR-025**: The documentation MUST note the GitHub App path as the direction of travel, citing
  the existing AppAuth foundations.

**Keeping the document true**

- **FR-026**: Claims in the threat model that correspond to checkable properties of the code MUST
  be covered by tests, so the document cannot silently drift away from the system it describes.
  At minimum this covers the guard's behaviour, the warning's trigger condition, and the bind
  defaults.

### Key Entities

- **Trust boundary**: A place where data or control crosses from one party's authority into
  another's, with a stated assumption about what is trusted across it.
- **Permitted origin set**: The origins a browser may present on a mutating request, derived from
  configuration.
- **Permitted host set**: The hosts any request may present, derived from configuration, plus any
  hostname the operator has explicitly permitted.
- **Mutating request**: A request using a method that changes state. The guard's origin check
  applies to these; its host check applies to everything.
- **Residual risk**: A risk the project has chosen not to mitigate, recorded so a reader can make
  their own decision rather than inferring safety from silence.
- **Token permission row**: One feature mapped to the fine-grained permission and access level it
  requires, and whether it is needed for a minimal run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reader who has not seen coordinare can name all four trust boundaries and state
  one unmitigated risk after reading the threat model once.
- **SC-002**: One hundred percent of the dashboard's mutating routes reject a foreign origin, and
  one hundred percent of all its routes reject a foreign host, verified by test rather than by
  inspection.
- **SC-003**: A route added after this feature is guarded without its author taking any action.
- **SC-004**: A request with no origin header, and a request from the dashboard's own page, both
  still succeed. The events stream still works.
- **SC-005**: Starting with a non-loopback dashboard bind produces a warning naming the absence
  of authentication; starting on loopback produces none.
- **SC-006**: The health server's bind is configurable, and its default is documented alongside
  the dashboard's.
- **SC-007**: An operator can determine the exact set of token permissions their intended feature
  set needs from the matrix alone, without running coordinare to discover a missing one.
- **SC-008**: Every checkable claim the threat model makes about guard behaviour, warning
  behaviour, and bind defaults has a corresponding test.
- **SC-009**: No existing dashboard route changes behaviour for a legitimate local caller, and no
  existing continuous-integration workflow changes its triggers or required status.

## Assumptions

- The dashboard remains unauthenticated in this feature. Authentication is spec 143 (#197). This
  work documents that posture and closes the CSRF and rebinding holes around it; it does not add
  a login.
- Rejecting a request is preferable to sanitising it. The guard refuses rather than attempting to
  interpret a suspicious header.
- The verified state of the code as of 2026-08-27 is the baseline: 24 mutating dashboard routes;
  a request-logging middleware already present to follow as a pattern; the health application
  bound to all interfaces with a hardcoded address while the dashboard defaults to loopback; the
  health application serving only read-only routes, one of which discloses operational metrics.
- **Nine** existing test files, carrying **sixteen** client constructions between them, need
  updating so their requests present a permitted host. This is deliberate scope, not incidental
  breakage, and those files belong to other specs. The figure was revised twice during
  implementation (6/13, then 9/16) because the first enumerations globbed only `tests/unit/*.py`
  and missed `tests/unit/dashboard/`, `tests/contract/`, and `tests/integration/`. Tests driving
  the **health** application are unaffected, because the health application is not guarded, and
  `tests/unit/dashboard/test_webhook.py` is unaffected because it builds a bare application rather
  than the dashboard one.
- The threat model describes the system as it is, including its weaknesses. It is not a marketing
  document, and a reader who finds it reassuring should find that reassurance earned.

## Out of Scope

- Dashboard authentication and authorization (spec 143, issue #197).
- Removing internal infrastructure references from examples and configuration (spec 145, issue
  #199).
- Making the repository public.
- Any change to how performers are sandboxed, or to the `docker.sock` mount itself. This feature
  documents that trust decision; changing it is a much larger piece of work.
- Rate limiting, request authentication, or audit logging for the dashboard.
