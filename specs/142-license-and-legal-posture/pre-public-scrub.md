# Pre-Public Scrub Checklist

**Feature**: 142-license-and-legal-posture | **Executed**: 2026-08-27
**Enforces**: FR-021, FR-022, FR-023, and FR-016's sequencing

The checklist that must pass before `ViviDynamics/coordinare` is made public. Every item names a
concrete command or UI step rather than an intention, and records what actually happened.

## How to read the timing column

| Timing | Meaning |
|---|---|
| `now` | Doable on a private repository. Executed as part of this feature. |
| `at_flip` | **Impossible until the repository is public.** Not part of this feature's done definition (SC-008), and enumerated below so a fully ticked list cannot be mistaken for readiness. |

## Secrets and credentials

| Check | Scope | Timing | Outcome |
|---|---|---|---|
| `git check-ignore .env .gh_token '*.pem' '*.key'` | working tree | `now` | **Gap found and closed.** `.env` was covered; `.gh_token`, `*.pem`, `*.key` were not. Added `.gh_token`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `credentials.json` to `.gitignore` and re-verified all are now ignored. |
| `git log --all --follow -- .env .gh_token` | full history | `now` | **Clean.** Zero commits have ever touched either file. The working token lives outside the repository (`../../.gh_token`), so it was never a candidate for commit; the `.gitignore` entries above guard against a stray in-repo copy. |
| `git ls-files \| grep -iE '\.pem$\|\.key$\|id_rsa\|credentials\.json\|\.gh_token'` | tracked files | `now` | **Clean.** Only `.env.example` matches the broader credential-filename sweep, and it is an intentional template with no values. |
| `git grep -ilE 'ghp_…\|github_pat_…\|sk-ant-…\|sk-…\|AKIA…\|BEGIN.*PRIVATE KEY'` | tracked content | `now` | **7 files match, all test fixtures. No real credential.** Each distinct matched value was extracted and inspected individually rather than the file list being waved off: `AKIAIOSFODNN7EXAMPLE` (AWS's own documented example key), `ghp_1234567890abcdefghijklmnopqrstuvwxyz`, `ghp_abcdefghijklmnopqrstuvwxyz1234567890`, `ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345`, `ghp_realtokenvalue1234567890`, `sk-ant-super-secret-key-1234567890abcdefghij`. The PEM matches are a constant named `_FAKE_PEM` and truncated header strings in config tests. |
| Same patterns across all history diffs | full history (20,768 objects) | `now` | **155 matching diff lines, all accounted for.** They are the same fixture values above, appearing across the commits that introduced and edited those tests. No distinct real-looking value exists in history. |
| Rotate any credential that was ever exposed | external | `now` | **Not applicable.** Nothing was exposed, so there is nothing to rotate. Recorded explicitly so a reader does not assume the step was skipped. |

## Repository settings

| Check | Scope | Timing | Outcome |
|---|---|---|---|
| Actions secrets inventory (`gh api …/actions/secrets`) | settings | `now` | **Zero secrets configured.** Nothing to leak through workflow logs today. Spec 152 ([#210](https://github.com/ViviDynamics/coordinare/issues/210)) would introduce the first one. |
| Enable **private vulnerability reporting** | settings | **`at_flip`** | **Cannot be done yet.** The feature exists on public repositories only, and this repository is private (`"visibility": "private"`, confirmed 2026-08-27; `GET …/private-vulnerability-reporting` returns 404). It also requires the **GitHub web UI**: the available tokens cannot write repository administration settings, which surfaces as a 404 on the write endpoint rather than a 403. `SECURITY.md` points at this route, so **enable it in the same sitting as the visibility flip**, or the published policy names a channel that does not exist. |
| Enable secret scanning and push protection | settings | `at_flip` | **Recommended, not required by this feature.** Currently disabled; both are free on public repositories. Hardening posture belongs to spec 144 ([#198](https://github.com/ViviDynamics/coordinare/issues/198)), so this is a pointer rather than an annexation. |
| Confirm branch protection still blocks direct pushes to `main` | settings | `at_flip` | Verify after the flip. Ruleset changes need the UI for the same token reason. |

## Internal infrastructure references

**Owner: spec 145 ([#199](https://github.com/ViviDynamics/coordinare/issues/199)), not this feature** (FR-022). Counted here so 145 starts with a measured scope rather than a guess, and so this checklist can state plainly what remains outstanding.

| Check | Tracked files matching | Timing | Outcome |
|---|---|---|---|
| Private RFC-1918 addresses outside `specs/` | ~~0~~ **12** | `at_flip` | **CORRECTED 2026-08-28. The original "0" was wrong, and the check that produced it was broken.** The grep used `\b` word boundaries, which `git grep -E` does not support (POSIX ERE), so the pattern matched nothing and was recorded as clean. Re-run without `\b`: **12 tracked non-spec files contain `192.168.3.30`**, the internal model host, including `routing.example.yaml` (which users copy), `agent/performer/src/performer/proxy/shim.py`, `scripts/pull_spark_models.sh`, and 8 performer proxy tests. Outstanding, owned by spec 145. |
| `spark/*` internal model identifiers outside `specs/` | **48** | `at_flip` | **Outstanding, owned by spec 145.** These are internal model-roster identifiers in examples, defaults, and routing configuration. |
| `litellm.vividynamics.com` internal gateway hostname outside `specs/` | **8** | `at_flip` | **Outstanding, owned by spec 145.** |

Historical `specs/` documents are excluded from the counts above deliberately: they are a record of past work, and rewriting them would falsify the project's own history. Whether to publish the `specs/` tree at all is a separate decision for the flip, and it is a larger one than the exclusion above might suggest. Measured 2026-08-27:

| `specs/` content | Files |
|---|---|
| Tracked spec files, total | 1,999 |
| Carrying `spark/*` internal model identifiers | **660** |
| Carrying the internal gateway hostname | **158** |
| Carrying private RFC-1918 addresses | 0 |

No credentials and no private addresses, so this is not a secrets problem. What it is: a detailed public record of the internal model roster, the gateway naming, and every design decision taken along the way. That may be perfectly acceptable, or even useful as evidence of engineering rigour. The point is that it is a deliberate disclosure choice at a scale worth knowing before making it, rather than something to discover after the flip.

## Legal and licensing

| Check | Scope | Timing | Outcome |
|---|---|---|---|
| `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md` present at root | working tree | `now` | **Done.** Enforced by `tests/unit/test_142_license_and_legal_posture.py`. |
| Dependency licence audit recorded and passing | working tree | `now` | **Done.** See [dependency-license-audit.md](./dependency-license-audit.md): 64 Tier 1, 4 Tier 2 each with a rationale, **0 rejected**. |
| No document claims coordinare is OSI open source | working tree | `now` | **Done and enforced.** The file-scoped wording guard fails the suite on any such claim. |
| No document implies a warranty, support obligation, or response commitment | working tree | `now` | **Done and enforced** (FR-026). |
| Packaging metadata declares the ELv2 reference, with no OSI classifier | working tree | `now` | **Done.** Verified by building both distributions: the wheel metadata carries `License-Expression: LicenseRef-Elastic-License-2.0`, and the root wheel bundles `LICENSE` and `NOTICE` under `dist-info/licenses/`. |

## Correction, 2026-08-28

The private-address row above originally read **0 files, Clean**. That was false, and the way it
was false is worth recording, because the same mistake is easy to repeat.

The evidence was a `git grep -ilE` pattern using `\b` word boundaries. `git grep -E` uses POSIX
ERE, which has **no `\b`**, so the expression matched nothing and produced a confident zero. The
scrub then recorded that zero as a clean result.

A check that silently matches nothing is indistinguishable from a check that passes. The
correction is not merely the number: it is that a scrub item whose evidence is a grep must be
verified to actually match something known-present before its zero is trusted. Spec 145 adds an
automated guard for internal references so this stops depending on anyone running the right
regex by hand.

## Outstanding at the time of writing

A fully ticked checklist above does **not** mean the repository is ready to publish. These remain:

1. **Enable private vulnerability reporting** at the flip, in the UI. `SECURITY.md` depends on it.
2. **Spec 145 ([#199](https://github.com/ViviDynamics/coordinare/issues/199))** must remove the internal references from non-spec files: **48** carrying `spark/*` model identifiers, **8** carrying the internal gateway hostname, and **12** carrying the `192.168.3.30` model-host address (the last of these missed entirely by the original scrub, see the correction above).
3. **Decide whether the `specs/` tree is published.** 660 of 1,999 spec files carry `spark/*` model identifiers and 158 carry the internal gateway hostname (no credentials, no private addresses). Not a secrets problem, but a disclosure choice at a scale nobody has weighed yet.
4. **Spec 144 ([#198](https://github.com/ViviDynamics/coordinare/issues/198))** threat model, which `SECURITY.md` reserves a section for and which is the other launch-blocking gate.
5. **Live-verify the external-contribution automation** with a pull request from an account outside the organization. It cannot be tested before merge.
