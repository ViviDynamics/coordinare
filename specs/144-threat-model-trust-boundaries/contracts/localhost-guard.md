# Contract: The Localhost Guard

**Feature**: 144-threat-model-trust-boundaries | **Enforces**: FR-009 through FR-018

The guard's complete decision table. The test module implements exactly this; the threat model
describes exactly this. If the three ever disagree, this file is the one to fix first.

## Two checks, two different attacks

| Check | Applies to | Attack it stops | Why the other check cannot stop it |
|---|---|---|---|
| `Origin` | Mutating methods only | Cross-site request forgery | `Host` is legitimately `localhost` in a CSRF attack, because the browser really is talking to localhost. Only `Origin` reveals that the *page* is foreign. |
| `Host` | **Every** method, reads included | DNS rebinding | A browser sends no `Origin` on a same-origin GET. Under rebinding the browser believes the attacker's page and the dashboard share an origin, so the origin check sees nothing and reads leak. |

Getting this wrong in either direction is a real hole. Checking `Origin` alone leaves
`GET /api/config/global` exfiltratable. Checking `Host` alone leaves mutations open to any page
that can reach loopback.

## Decision table

Evaluated in order. First match wins.

| # | Condition | Result | Requirement |
|---|---|---|---|
| 1 | `Host` absent | **reject** | HTTP/1.1 requires it; its absence is malformed or hostile |
| 2 | `Host` not in the permitted host set | **reject** | FR-011 |
| 3 | Method is not mutating (`GET`/`HEAD`/`OPTIONS`) | **allow** | FR-011 is already satisfied by 1-2 |
| 4 | `Origin` absent | **allow** | FR-010 |
| 5 | `Origin` not in the permitted origin set | **reject** | FR-009 |
| 6 | otherwise | **allow** | |

**Row 4 is the one that looks wrong and is not.** Allowing an absent `Origin` is correct because
browsers always send `Origin` on non-GET requests, so its absence means the caller is not a
browser, and cross-site request forgery requires a browser. Rejecting it would break curl,
scripts, monitoring probes, and the project's own test client, for no security gain. Row 2 still
applies to those callers, so they are not unguarded.

## Permitted sets

Computed **once at application construction** (FR-013, and Principle IV: no per-request I/O).

**Hosts** — any of:

| Source | Example |
|---|---|
| Any address parsing as a loopback IP | `127.0.0.1`, `127.0.0.2`, `::1` |
| The literal name | `localhost` |
| The configured `dashboard_host` | whatever the operator set |
| Each entry in `trusted_dashboard_hosts` | operator's proxy hostname (FR-015) |

Loopback is decided with `ipaddress.is_loopback`, not a list of spellings, so the whole
`127.0.0.0/8` range and `::1` are covered without anyone maintaining a list (research D2).

**Origins** — scheme `http` or `https`, host from the set above. Port is not considered.

**Port rule: the port is ignored entirely** (revised 2026-08-28). The first version compared it
against `dashboard_port`, which was wrong on both counts.

It added no security. A `Host` port is set by the browser from the URL it connected to, and a
request cannot arrive on a port the server is not listening on, so a mismatched port can only come
from a non-browser client, which this guard never authenticated anyway. Rebinding is caught by the
hostname check.

It broke real deployments. Any app served on a port other than the configured one refused every
request. The end-to-end browser suite binds a random free port, so the browser sent
`Host: 127.0.0.1:<random>`, every page load returned 403, and the CI job hung until cancelled.
Proven by re-introducing the comparison: `GET / → 403` and the first browser test fails in
4.5 seconds; without it, 98 end-to-end tests pass.

A consequence worth stating: a page served from **another port on the same machine** counts as a
permitted origin. That follows the guard's own model, which the threat model states plainly. This
is a locality check, not authentication, and anyone who can make requests from your machine
already has full control. Treating `localhost:9999` as hostile while trusting `localhost:8090`
would draw a line the rest of the design does not draw.

## Parsing rules that will otherwise produce bugs

1. **IPv6 in `Host` is bracketed**: `[::1]:8090`. Strip brackets before parsing as an address.
2. **Never `rsplit(":", 1)` blindly to remove a port.** That corrupts a bare IPv6 address such as
   `::1`. Split after the closing bracket when present, and on the last colon only when the value
   contains exactly one.
3. **Compare hosts case-insensitively.** Hostnames are not case-sensitive; `LOCALHOST` is valid.
4. **Do not trust `X-Forwarded-Host`.** It is attacker-controlled unless a proxy overwrites it,
   and honouring it by default would silently undo the guard. Operators name their hostname in
   `trusted_dashboard_hosts` instead (research D3).

## Rejection response

A rejection returns **403** with a body naming **which header** failed and what was expected
(FR-016). It must not echo the rejected value back unescaped into an HTML context.

The distinction matters operationally: an operator who put the dashboard behind a proxy and
forgot `trusted_dashboard_hosts` gets a message telling them exactly that, rather than an
unexplained failure they debug for an hour.

## Fail-closed property

The guard is middleware, so a route added tomorrow is covered without its author doing anything
(FR-012). The test asserts this **structurally** — by enumerating the application's actual routes
and checking each mutating one rejects a foreign origin — rather than against a fixed list of
paths, which would itself go stale and defeat the purpose.

The constructed application has **23** mutating routes. A 24th mutating decorator exists in
`dashboard.py` but belongs to the webhook route, which `__main__` registers separately and only
when webhooks are enabled.

## The one exemption (FR-027)

`exempt_paths` removes a path from **both** checks. It exists for exactly one route today.

The GitHub webhook endpoint is *designed* to be called from the internet. GitHub is not a local
caller, so the host check would refuse every delivery and webhooks would stop working entirely.
That route carries its own authentication: an HMAC signature over the body, verified against a
shared secret, rejecting with 401.

Two rules keep this from becoming a general-purpose hole:

1. **Anything exempted must authenticate itself.** An exemption without authentication is simply
   an unguarded route, which is what this guard exists to prevent.
2. **Exemptions are exact path matches, never prefixes**, so one entry cannot silently widen to
   cover a subtree.

The distinction the test asserts: an exempt route reached with a foreign `Host` returns **401**
from its own authentication, not **403** from the guard. Exempt from the guard is not the same as
unauthenticated.

## Explicitly not in scope

- Authentication of any kind. A permitted local caller is fully trusted, which is the current
  posture and is spec 143's (#197) to change.
- Rate limiting.
- CSRF tokens. The origin check is sufficient for a loopback control plane and needs no state.
