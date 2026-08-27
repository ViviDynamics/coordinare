# Phase 0 Research: License and Legal Posture

**Feature**: 142-license-and-legal-posture | **Date**: 2026-08-27

Seven decisions. R5 and R6 changed the plan; the rest confirm approach.

---

## R1: Obtaining verbatim ELv2 text

**Decision (corrected 2026-08-27 on contact with the real text)**: Commit the canonical ELv2
text **unmodified**. There are no designation fields to fill.

**What the original decision got wrong**: it assumed ELv2 carries a fill-in parameter block
(Licensor, Licensed Work, Additional Definitions). That is the **Business Source License's**
structure. ELv2's text is generic throughout: "The **licensor** is the entity offering these
terms, and the **software** is the software the licensor makes available under these terms."
Nothing is parameterised, so editing the body to name Vivi Dynamics LLC would break the verbatim
requirement rather than satisfy it. Identification moves to `NOTICE`, plus an optional attribution
header kept clearly outside the license body.

This is exactly the error the original decision was written to prevent, and it was caught by
following that decision's own instruction to fetch the text rather than trust recall.

**Rationale**: FR-001 requires the text be verbatim. A license reproduced from recall is the
kind of error nobody notices until it matters, and copying another project's `LICENSE` risks
inheriting their designations or their edits. ELv2 is explicitly designed to be copied with
three parameters supplied: Licensor, The Licensed Work, and Additional Definitions if any.

**Where identification actually lives**:

| What | Where | Value |
|---|---|---|
| Copyright holder | `NOTICE` | Vivi Dynamics LLC |
| Licensed work | `NOTICE` | coordinare |
| Attribution (optional) | `LICENSE` header, outside the body | coordinare is licensed by Vivi Dynamics LLC under the Elastic License 2.0 |
| Licensor (in the license body) | nowhere, by design | ELv2 says "the entity offering these terms" |

**Canonical source** (retrieved 2026-08-27): Elastic's own plaintext copy at
`https://raw.githubusercontent.com/elastic/elasticsearch/main/licenses/ELASTIC-LICENSE-2.0.txt`,
93 lines / 3860 bytes, nine sections, carrying its own `URL:` pointer back to
`https://www.elastic.co/licensing/elastic-license`. This is the license steward's own repository
rather than a third-party adopter's copy, which was the risk the original decision named.

**Note**: "Vivi Dynamics LLC" is the official business entity name (confirmed 2026-08-27), not
the "ViviDynamics" trade style used for the GitHub organization. `LICENSE` and `NOTICE` are legal
documents and carry the entity name; informal prose may use either.

**Verification**: The test asserts the file contains ELv2's distinctive limitation clause and
contains no unfilled placeholder tokens. It deliberately does not diff against a checked-in
copy of the license, which would just be the same text twice.

**Alternatives considered**: Reproducing from memory (rejected: accuracy is the whole
requirement). Copying from another ELv2 project such as Elasticsearch (rejected: risks
inheriting their designations, and their copy may itself have drifted).

---

## R2: Designing the wording guard without false positives

**Decision**: Scope the check to an explicit set of **public-facing posture files** and, within
only those files, forbid `open source`, `open-source`, and `OSI` entirely. Separately assert
that `source-available` appears in the README's licensing section. Where a future legitimate
third-party mention is needed inside the scoped set, the test carries an explicit allowlist of
`(file, matched text)` pairs that must be extended deliberately.

**Scoped set**: `README.md`, `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, and
everything under `.github/` that is user-visible (issue templates, pull request template).

**Rationale**: This is the design that makes the guard both strict and honest. A blanket
repository-wide ban on the phrase would fire today on two accurate, unrelated statements:
`docs/opencode-sdk.md` describing OpenCode (someone else's software) as open source, and
`src/coordinare/services/scoring.py:102` commenting that small open-source models drop JSON
envelopes. Neither is a claim about coordinare. Attempting to distinguish subject from object
with a regex ("is coordinare the thing being described?") is the sort of cleverness that fails
silently in both directions. File scoping is dumb, total within its scope, and produces exactly
zero false positives, because the posture files are precisely the ones where the phrase can only
ever be a claim about coordinare.

**Consequence worth accepting**: if the README ever wants to describe a third-party tool as open
source, the test fails and the author must add an allowlist entry. That is the correct friction:
it forces a conscious look at wording in the one file most likely to be quoted back at us.

**Alternatives considered**: Repository-wide ban (rejected: two immediate false positives).
Proximity matching between "coordinare" and the phrase (rejected: brittle, and fails open on
paraphrase such as "this project is open source"). Manual review only (rejected: Principle II,
and the wording is exactly the sort of thing that rots on a later edit).

---

## R3: Reproducible dependency license data

**Decision**: Derive the distributed set by walking `uv.lock` with stdlib `tomllib` from each
distributed project's declared runtime dependencies, then read each package's license from
installed metadata via stdlib `importlib.metadata`. Both the committed audit and the FR-020
test use the same derivation, so the document and the check cannot disagree.

**Verified feasible** (2026-08-27): `uv.lock` holds 108 packages. Walking transitively from
`coordinare`'s 18 declared runtime dependencies yields **63 packages**. The remaining 45 are
dev-only and correctly excluded, since development dependencies are not conveyed to recipients.
`agent/performer/uv.lock` exists separately and must be walked on its own; the root lock's
`performer` entry lists no dependencies.

**License field precedence** (packaging metadata is inconsistent in practice, so order matters):
`License-Expression` (PEP 639, the modern SPDX field), then a short free-text `License`, then a
`License ::` classifier. Long free-text `License` values are frequently the entire license text
pasted into a field and must not be treated as an identifier.

**Rationale**: Zero new dependencies, which matters more than usual in a feature whose entire
subject is dependency licensing. Adding `pip-licenses` to audit licenses would be a small joke
at our own expense. The lock file is the correct source of truth for the transitive closure;
scanning the whole virtualenv would sweep in dev tooling and misreport the distributed set.

**Alternatives considered**: `pip-licenses` (rejected: a new dependency, for something 30 lines
of stdlib does). Scanning all installed distributions (rejected: conflates dev with distributed,
which is the distinction the audit exists to draw). Hand-transcribing the list (rejected: stale
the moment a lock file changes, and FR-020 needs machine-readable data anyway).

---

## R4: External pull request automation mechanics

**Decision**: One new workflow, `pull_request_target` triggered on `opened` and `reopened`,
running on a GitHub-hosted runner, with `permissions: pull-requests: write` and nothing else.
It calls the GitHub API only. It never runs `actions/checkout`.

**Rationale, and why `pull_request` cannot work**: a fork-originated `pull_request` event gets a
read-only `GITHUB_TOKEN` and no access to secrets, so it physically cannot comment, label, or
close. `pull_request_target` runs in the base-repository context with a write token, which is
what the job needs and also what makes it dangerous if misused. The danger is specifically
checking out and executing untrusted head code with a privileged token. Since this workflow
reads three event fields and makes three API calls, never touching the fork's contents, it sits
on the safe side of that line. This is the concrete mechanism behind FR-013.

**Author relationship** (FR-011, FR-012): use `github.event.pull_request.author_association`,
which GitHub computes and the author cannot influence.

| Treated as internal, no action | Treated as external, policy applies |
|---|---|
| `OWNER`, `MEMBER`, `COLLABORATOR` | `NONE`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER` |

Bots are excluded separately via `github.event.pull_request.user.type == 'Bot'`, which covers the
existing version-sync automation and any future Dependabot.

`CONTRIBUTOR` is classified external deliberately. It means "has previously landed a commit",
which under a closed-contribution policy should not describe an outsider, but if it ever does,
the policy still applies.

**Runner choice**: every existing workflow targets `[self-hosted, linux]`. An externally
triggered workflow should not touch the runner host, so this one uses a GitHub-hosted runner. If
the organization has no GitHub-hosted minutes available, the fallback is self-hosted **with the
absolute constraint that the job only calls the API**, which this job already satisfies. Flagged
for confirmation during implementation.

**Security carve-out** (FR-025): keyword match over title and body for security terms
(`security`, `vulnerabilit`, `CVE`, `exploit`, `injection`, `RCE`, `XSS`, `CSRF`, `credential`,
`token leak`). On a hit: comment and label, but **do not close**. The match runs over
attacker-authored text, which is acceptable here precisely because its only power is to
*suppress* automation and escalate to a human. A false positive costs one manual close.

**Alternatives considered**: `pull_request` with a follow-up scheduled job (rejected: needless
indirection). A GitHub App (rejected: infrastructure for a three-call job). Branch protection
alone (rejected: blocks the merge but leaves the author with no explanation).

---

## R5: Private vulnerability reporting cannot be enabled yet (**changed the plan**)

**Finding**: GitHub private vulnerability reporting is available on **public repositories only**.
This repository is private (`"visibility": "private"`, confirmed 2026-08-27), and
`GET /repos/ViviDynamics/coordinare/private-vulnerability-reporting` returns 404.

**Decision**: FR-016 is not satisfiable during this feature. It moves to the pre-public scrub
checklist as a step performed **at flip time**, and `SECURITY.md` is written so its primary route
becomes live the moment the repository goes public. The scrub checklist gains an explicit
sequencing note so this cannot be forgotten: publishing `SECURITY.md` while the reporting route
is disabled would advertise a channel that does not exist.

**Additional constraint**: enabling it needs the GitHub web UI. Per the established pattern in
this repository, neither available token can write repository administration settings (the
classic symptom is a 404 on the write endpoint, which is how GitHub hides endpoints from
under-scoped tokens, and is already documented for rulesets). This is a human step, not an
automatable one, and the checklist says so.

**Related, same finding**: `security_and_analysis` shows secret scanning, push protection, and
Dependabot security updates all **disabled**. Secret scanning is free on public repositories and
is squarely a "before you go public" concern, so the scrub checklist recommends enabling it at
flip time. Deliberately kept as a checklist recommendation rather than a requirement of this
feature: hardening posture belongs to spec 144 (#198), and this feature should not annex it.

---

## R6: `semgrep` is redistributed but not linked (**changed the plan**)

**Finding**: `semgrep` (LGPL-2.1-or-later) is not a declared Python dependency of any project
here. It is installed into the performer image by `agent/performer/Dockerfile.full` and invoked
as a **subprocess** by `src/coordinare/services/security_scanner.py:148`.

**Decision**: In scope for the audit as redistributed content of the performer image; out of
scope as a combined work. The audit records it with that distinction explicit.

**Rationale**: These are two independent questions and conflating them is the usual error.
Invoking a separate executable is aggregation, not linking, so LGPL-2.1's obligations do not
reach coordinare's own source. But the image genuinely bundles the binary, and shipping it is
distribution, which carries notice and source-availability obligations for semgrep itself.
Both are satisfied trivially: semgrep is published on PyPI and GitHub, and we do not modify it.

**Consequence for FR-019a**: this is the case that justifies recording *how* each exception is
consumed, not merely that it is present. A package imported into our process and a package
executed as a subprocess sit in genuinely different positions, and an audit that flattens them
is less useful than one that does not.

---

## R7: License declaration format across three pyproject files

**Decision**: Use PEP 639 in all three: `license = "LicenseRef-Elastic-License-2.0"` with
`license-files = ["LICENSE", "NOTICE"]` where the build backend supports it. Add no OSI
`License ::` classifier.

**Rationale**: ELv2 has no SPDX identifier because SPDX lists OSI and FSF licenses; PEP 639's
`LicenseRef-` prefix is the specified way to name a non-SPDX license, so this is the intended
mechanism rather than a workaround. Omitting the classifier is deliberate and load-bearing:
every `License ::` classifier asserts an OSI-approved license, so adding one would state in
machine-readable metadata exactly the claim FR-006 forbids in prose. Getting this wrong would be
the most consequential possible instance of the error this feature exists to prevent, since
metadata is what indexers and license scanners read.

**Verification**: the test asserts each of the three files declares a license, and that none
carries a `License ::` classifier. The three paths (`pyproject.toml`,
`agent/performer/pyproject.toml`, `packages/service_inference/pyproject.toml`) currently declare
neither, confirmed 2026-08-27.

**Alternatives considered**: Free-text `license = {text = "..."}` (rejected: deprecated by PEP
639). `Private :: Do Not Upload` (rejected: means something else, and coordinare may legitimately
be published). No declaration (rejected: FR-003, and unset metadata reads as "unknown", which
the audit's own check treats as a rejection).
