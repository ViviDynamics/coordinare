# Contract: Accepted License Set

**Feature**: 142-license-and-legal-posture | **Decided**: 2026-08-27 | **Enforces**: FR-019, FR-019a, FR-020

This is the contract the FR-020 check enforces and the audit records against. It is stated here
so the rule has one home; the test imports the same data rather than restating it.

## Tier 1: Permitted without comment

Distributed packages under any of these ship with no further action.

```
MIT
BSD-2-Clause
BSD-3-Clause
Apache-2.0
ISC
PSF-2.0
Unlicense
Zlib
```

Plus any expression whose SPDX evaluation lands here. **`AND` and `OR` are not
interchangeable, and treating them as such is a correctness bug rather than a nicety:**

- **`OR` is disjunctive.** The recipient elects one term, so the expression is as permissive as
  its easiest alternative. `Apache-2.0 OR MIT` is Tier 1. So is `GPL-3.0 OR MIT`, because
  nothing compels the recipient to take the GPL option.
- **`AND` is conjunctive.** Every operand's obligations apply at once, so the expression is as
  restrictive as its hardest operand. `MPL-2.0 AND (Apache-2.0 OR MIT)`, which is what `orjson`
  actually publishes, is **Tier 2** and not Tier 1: the MPL-2.0 obligation binds regardless of
  the permissive half. `AND` binds tighter than `OR`, per SPDX.

Scanning an expression for any acceptable token would let `GPL-3.0 AND MIT` through, which is
precisely the class of dependency this contract exists to reject. (Corrected 2026-08-27: the
first draft of this contract made exactly that error, and it was caught by reading the generated
audit rather than by the check passing.)

Historical spellings of the same licenses appear in real metadata (`BSD License`,
`Apache Software License`, `MIT license`, `3-Clause BSD License`) and normalize into this tier.

**Bare family names.** Some packages publish only a family, with no variant: `xxhash` declares
`BSD` and ships no license file. Every BSD variant is permissive and ELv2-compatible, so the
family is accepted as Tier 1, and the audit records that the variant is unspecified upstream.
This is deliberately *not* aliased to `BSD-3-Clause`, which would invent precision the metadata
does not have.

## Tier 2: Permitted as a named exception, rationale required

```
MPL-2.0
LGPL-2.0, LGPL-2.1, LGPL-3.0 (and -only / -or-later variants)
```

Each package under a Tier 2 license MUST carry its own written rationale in the audit, recording
that it is redistributed unmodified and **how it is consumed** (imported into our process, or
invoked as a separate executable). An exception without a rationale fails the check.

Tier 2 packages as actually present, verified 2026-08-27 against the runtime closure:

| Package | License | Consumed as | Distributed via |
|---|---|---|---|
| `psycopg` | LGPL-3.0-only | import | pypi_dependency |
| `psycopg-pool` | LGPL-3.0-only | import | pypi_dependency |
| `certifi` | MPL-2.0 | import | pypi_dependency |
| `orjson` | MPL-2.0 AND (Apache-2.0 OR MIT) | import | pypi_dependency |
| `semgrep` | LGPL-2.1-or-later | subprocess | container_image |

`pathspec` (MPL-2.0) appeared in an earlier draft of this table and has been removed: it is
**development-only**, reaching the environment through the formatter rather than through either
project's runtime dependencies, so it is never conveyed to a recipient and carries no
distribution obligation. It is a useful illustration of why the closure is walked from declared
runtime dependencies rather than from whatever happens to be installed.

## Metadata that cannot classify a package

Some packages publish a `License` value that names the *arrangement* rather than a licence.
`python-dateutil` declares `Dual License`: short enough to look like an identifier, and
informative about nothing. Such values are refused and the package is resolved **by hand**,
by reading the licence text it actually ships, with the result and its verification recorded
in the checker's `VERIFIED_LICENSES` table. The audit reports the metadata field as `verified`
so these are visible rather than blended in.

**The inference this deliberately does not make.** It is tempting to read several
`License ::` classifiers as "dual licensed" and OR them together — and for `python-dateutil`
that happens to give the right answer. `orjson` shows why it is unsound: it publishes Apache,
MIT *and* MPL-2.0 classifiers, while its actual expression is `MPL-2.0 AND (Apache-2.0 OR MIT)`,
which is Tier 2. OR-ing them would drop a real MPL obligation and promote it to Tier 1. Since
`OR` resolves to its most permissive operand, a wrong guess fails **open**, so this is exactly
the place not to guess.

## Platform-conditional packages

A package reachable only through a marker-gated edge may legitimately be absent from the
environment where the check runs. `tzdata` is the live example: reachable only via
`psycopg -> tzdata` under `sys_platform == 'win32'`.

Such packages are **not verified** by the automated check on a platform where they are not
installed. That is a real limitation, not a rounding error, and the contract's response is to
require that every one of them be listed explicitly in the audit. The gap is then visible to a
reader rather than hidden behind a passing test.

## Tier 3: Rejected

```
GPL-1.0, GPL-2.0, GPL-3.0 (and -only / -or-later variants)
AGPL-1.0, AGPL-3.0 (and variants)
SSPL-1.0
UNKNOWN  (metadata absent or unreadable)
```

**Why GPL-family terms are a real conflict rather than a cautious exclusion**: the GPL forbids
imposing further restrictions on the rights it grants. ELv2's hosted-service limitation is
exactly such a restriction. Distributing GPL code as part of something offered under ELv2 puts
the two licenses in direct opposition, so this is not a matter of preference.

`UNKNOWN` is rejected rather than flagged. Unreadable metadata is indistinguishable from
unlicensed, and treating it as a soft warning is how an unlicensed package eventually ships.

## First-party exemption

`coordinare`, `performer`, and `coordinare-service-inference` are not judged against the tiers. They are
instead asserted to declare the ELv2 reference in their own packaging metadata (FR-003, FR-004).
All three currently resolve to `UNKNOWN`, which is the gap FR-003 exists to close, and is why
the check must special-case them rather than simply rejecting `UNKNOWN` everywhere.

## Change procedure

Adding a Tier 1 or Tier 3 entry, or moving a license between tiers, is a change to this contract
and requires the same review as a code change. Adding a Tier 2 *package* needs only its rationale
in the audit. That asymmetry is intentional: judging one more library is routine, and rewriting
the rule is not.
