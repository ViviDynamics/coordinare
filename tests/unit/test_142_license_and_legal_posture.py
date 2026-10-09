"""Spec 142 / issue #196 — license and legal posture for public release.

Covers the four stories that make the repository publishable under the Elastic
License 2.0:

  US1  a stranger can tell what they are allowed to do (LICENSE, NOTICE, README,
       packaging metadata)
  US2  a would-be contributor learns the policy before wasting effort
       (CONTRIBUTING, templates, external-PR automation)
  US3  a security researcher can report privately (SECURITY.md)
  US4  the maintainer can prove the repository is safe to publish (dependency
       licence audit, pre-public scrub)

The enforcement here exists because wording rots. Every assertion the spec makes
that can be checked mechanically is checked, so a later well-meaning edit cannot
quietly reintroduce a claim (FR-007) or a promise (FR-026) the project does not
intend to make.
"""

from __future__ import annotations

import contextlib
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

#: Public-facing documents subject to the wording guards (research R2).
#:
#: Deliberately scoped rather than repository-wide. The tree contains accurate
#: statements describing *other people's* software as open source, and a global
#: ban would fire on those. Inside this set the phrase can only ever be a claim
#: about coordinare, so a total ban here is both strict and false-positive free.
POSTURE_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/feedback.yml",
)

#: Files that must exist for the posture to be complete (FR-001/002/008/014).
REQUIRED_POSTURE_FILES = (
    "LICENSE",
    "NOTICE",
    "CONTRIBUTING.md",
    "SECURITY.md",
)

#: ``(path, matched text)`` pairs deliberately permitted despite matching a
#: forbidden pattern. Starts almost empty on purpose: the one entry is ELv2's own
#: warranty *disclaimer*, which must not be mistaken for an offer of warranty.
WORDING_ALLOWLIST: tuple[tuple[str, str], ...] = (
    # ELv2 disclaims a warranty. Disclaiming one is the opposite of offering one.
    ("LICENSE", "without any warranty or"),
    # ELv2's own cure period: a licensee who stops violating within 30 days has
    # their licenses reinstated. That is a term of the license granting the
    # *licensee* time, not a commitment by us to respond to anything.
    ("LICENSE", "later than 30 days after you receive that notice"),
    # SECURITY.md pointing out that the license disclaims warranties. Again a
    # disclaimer, not an offer; saying so out loud helps the reader.
    ("SECURITY.md", "the license disclaims all warranties"),
)

#: Claims that coordinare is OSI open source (FR-006, FR-007).
#:
#: Regex rather than substring: ``OSI`` is a three-letter acronym that occurs
#: inside ordinary words ("closing", "positioning"), and matching it as a
#: substring produced exactly the class of false positive research R2 warned
#: about. The phrases are matched with a flexible separator so "open source",
#: "open-source" and "open  source" are all caught.
FORBIDDEN_LICENSING_CLAIMS = (
    r"open[\s-]+source",
    r"\bOSI\b",
)

#: Warranty, support and response commitments (FR-026).
FORBIDDEN_COMMITMENT_PATTERNS = (
    r"\bwarrant(y|ies|ed)\b",
    r"\bguarantee",
    r"\bSLA\b",
    r"\bwe will respond\b",
    r"\bsupported versions?\b",
    r"\b\d+\s*(?:business\s*)?(?:hour|day|week|month)s?\b",
    r"\bwithin\s+\d+",
)

#: The nine canonical ELv2 section headings, in order (FR-001).
ELV2_SECTIONS = (
    "Acceptance",
    "Copyright License",
    "Limitations",
    "Patents",
    "Notices",
    "No Other Rights",
    "Termination",
    "No Liability",
    "Definitions",
)

LICENSOR = "Vivi Dynamics LLC"

PACKAGING_FILES = (
    "pyproject.toml",
    "agent/performer/pyproject.toml",
    "packages/service_inference/pyproject.toml",
)

ELV2_LICENSE_ID = "LicenseRef-Elastic-License-2.0"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _existing_posture_files() -> tuple[str, ...]:
    """Posture files that exist on disk.

    Presence is asserted separately, so the wording guards do not fail for the
    wrong reason while the documents are still being written.
    """
    return tuple(p for p in POSTURE_FILES if (REPO_ROOT / p).is_file())


def _allowlisted(path: str, matched: str) -> bool:
    lowered = matched.lower()
    return any(
        path == allow_path and allow_text.lower() in lowered
        for allow_path, allow_text in WORDING_ALLOWLIST
    )


def _readme_license_section() -> str:
    """The README's licensing section, from its heading to the next heading."""
    text = _read("README.md")
    match = re.search(r"^## License\b.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "README.md has no '## License' section"
    return match.group(0)


# ---------------------------------------------------------------------------
# US1 — a stranger can tell what they are allowed to do
# ---------------------------------------------------------------------------


def test_required_posture_documents_exist() -> None:
    """FR-001/002/008/014 — the four root documents a public repo needs."""
    missing = [p for p in REQUIRED_POSTURE_FILES if not (REPO_ROOT / p).is_file()]
    assert not missing, f"missing required posture documents: {missing}"


def test_license_holds_unmodified_elv2_text() -> None:
    """FR-001 — the ELv2 body is committed unmodified.

    ELv2 carries no designation fields (that is the Business Source License's
    structure), so "filling it in" is not a thing that can be done to it. The
    body must match Elastic's published text.
    """
    text = _read("LICENSE")

    assert "Elastic License 2.0" in text
    for section in ELV2_SECTIONS:
        assert f"## {section}" in text, f"LICENSE missing ELv2 section '{section}'"

    # The limitation that does the actual work for this project.
    assert "You may not provide the software to third parties as a hosted or managed" in text, (
        "LICENSE missing ELv2's hosted-service limitation"
    )

    # The warranty disclaimer and liability limitation.
    assert "without any warranty or" in text
    assert "will not be liable to you for any damages" in text

    # ELv2's generic licensor definition, proving the body was not rewritten.
    assert "is the entity offering these terms" in text


def test_license_body_carries_no_placeholders_or_injected_designations() -> None:
    """FR-001 — no placeholder stubs, and no designation line spliced into the body.

    A ``Licensor:`` line would mean someone treated ELv2 like the BSL and edited
    the licensed text, which is the specific error this test exists to catch.
    """
    text = _read("LICENSE")

    assert "{{" not in text, "LICENSE contains an unfilled template placeholder"
    assert not re.search(
        r"^\s*(?:Licensor|Licensed Work|Additional Definitions|Change Date)\s*:",
        text,
        re.MULTILINE,
    ), "LICENSE has a designation line spliced into the ELv2 body; ELv2 has no such fields"
    assert not re.search(
        r"\[\s*(?:licensor|licensed work|your name|company|entity)\s*\]",
        text,
        re.IGNORECASE,
    ), "LICENSE contains an unfilled bracketed designation stub"


def test_notice_identifies_licensor_and_work() -> None:
    """FR-002, FR-001a — NOTICE performs the identification ELv2's body does not."""
    text = _read("NOTICE")

    assert LICENSOR in text, f"NOTICE must name {LICENSOR!r} as copyright holder"
    assert "2026" in text, "NOTICE must carry the copyright year"
    assert "coordinare" in text.lower(), "NOTICE must identify coordinare as the licensed work"
    assert "LICENSE" in text, "NOTICE must point at LICENSE for terms"


def test_no_per_file_copyright_headers_were_introduced() -> None:
    """FR-002 — a single copyright statement, not a header on every file."""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "src" / "coordinare").rglob("*.py"))
        if re.search(
            r"^\s*#.*copyright", path.read_text(encoding="utf-8"), re.IGNORECASE | re.MULTILINE,
        )
    ]
    assert not offenders, f"per-file copyright headers found (FR-002 forbids them): {offenders}"


def test_posture_files_never_claim_coordinare_is_open_source() -> None:
    """FR-006, FR-007 — the wording guard, scoped to the posture files."""
    violations: list[str] = []
    for rel in _existing_posture_files():
        for lineno, line in enumerate(_read(rel).splitlines(), start=1):
            for claim in FORBIDDEN_LICENSING_CLAIMS:
                if re.search(claim, line, re.IGNORECASE) and not _allowlisted(rel, line):
                    violations.append(f"{rel}:{lineno}: {line.strip()!r} matched {claim!r}")
    assert not violations, (
        "posture files must never claim coordinare is open source or OSI-approved "
        "(use 'source-available'):\n" + "\n".join(violations)
    )


def test_wording_guard_scope_excludes_accurate_third_party_references() -> None:
    """FR-007 — zero false positives on statements about other people's software.

    This file legitimately describes third-party tools as open source. If it
    ever lands inside ``POSTURE_FILES`` the guard would start failing on
    accurate prose, which is how a guard gets weakened instead of fixed.

    Spec 173 removed the second anchor: ``services/scoring.py`` carried a
    comment about "small open-source models" and was deleted with the in-daemon
    advocate path. The remaining anchor is listed rather than replaced with an
    invented one, because prose written to satisfy a test is not evidence.
    """
    for rel in ("docs/opencode-sdk.md",):
        assert (REPO_ROOT / rel).is_file(), f"expected {rel} to exist"
        assert rel not in POSTURE_FILES, (
            f"{rel} accurately describes third-party software as open source and must "
            "stay outside the wording guard's scope"
        )


def test_posture_files_promise_no_warranty_support_or_response() -> None:
    """FR-026 — no implied warranty, support obligation, or response commitment.

    ELv2's own warranty *disclaimer* is allowlisted: disclaiming a warranty is
    the opposite of offering one.
    """
    violations: list[str] = []
    for rel in _existing_posture_files():
        for lineno, line in enumerate(_read(rel).splitlines(), start=1):
            for pattern in FORBIDDEN_COMMITMENT_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match and not _allowlisted(rel, line):
                    violations.append(f"{rel}:{lineno}: {line.strip()!r} matched {pattern!r}")
    assert not violations, (
        "posture files must not state or imply a warranty, support obligation, or "
        "response commitment (FR-026):\n" + "\n".join(violations)
    )


def test_readme_states_the_permissions() -> None:
    """FR-005 — what a user may do, in plain English."""
    section = _readme_license_section().lower()
    for phrase, label in (
        ("source-available", "the 'source-available' term"),
        ("self-host", "the right to self-host"),
        ("modify", "the right to modify"),
    ):
        assert phrase in section, f"README licensing section must state {label}"


def test_readme_states_the_prohibition_including_payment_irrelevance() -> None:
    """FR-005, FR-005a — the prohibition covers access, paid or unpaid."""
    section = _readme_license_section().lower()

    assert "hosted" in section or "managed" in section, (
        "README must state the hosted/managed service prohibition"
    )
    assert "third part" in section, "README must say the prohibition is about third parties"
    assert any(
        marker in section
        for marker in ("whether or not", "paid or unpaid", "even if", "free of charge", "no fee")
    ), (
        "README must state the prohibition applies whether or not money changes hands "
        "(FR-005a) — a free login is still provision of a hosted service"
    )


def test_readme_states_future_terms_and_contribution_model() -> None:
    """FR-005 — future versions may differ; contribution is by feedback.

    The future-terms sentence belongs to the licensing section. The contribution
    model is checked against the whole README, since FR-005 requires the README
    to state it and does not dictate which section carries it.
    """
    section = _readme_license_section().lower()
    assert "future" in section, "README must say future versions may carry different terms"

    readme = _read("README.md").lower()
    assert "issue" in readme, (
        "README must point readers at the issue tracker (feedback-not-pull-requests)"
    )
    assert "pull request" in readme and "not accepted" in readme, (
        "README must state that public pull requests are not accepted"
    )


def test_readme_places_a_commercial_route_beside_the_prohibition() -> None:
    """FR-024 — the prohibition never ships without a door next to it."""
    section = _readme_license_section()

    assert "vividynamics.com/contact" in section, (
        "README must offer a commercial-licensing route alongside the prohibition "
        "(FR-024), so a firm wanting to serve its own customers finds a door not a wall"
    )
    assert "commercial" in section.lower()


def test_readme_has_exactly_one_license_heading() -> None:
    """FR-005 — the pre-existing stub was replaced, not duplicated."""
    headings = re.findall(r"^## License\b.*$", _read("README.md"), re.MULTILINE)
    assert len(headings) == 1, f"expected exactly one '## License' heading, found {headings}"


def test_packaging_metadata_declares_the_elv2_reference() -> None:
    """FR-003 — a built distribution must not ship unset license metadata."""
    for rel in PACKAGING_FILES:
        data = tomllib.loads(_read(rel))
        declared = data.get("project", {}).get("license")
        assert declared == ELV2_LICENSE_ID, (
            f"{rel} must declare license = {ELV2_LICENSE_ID!r} (PEP 639; ELv2 has no "
            f"SPDX id, so LicenseRef- is the specified form), got {declared!r}"
        )


def test_packaging_metadata_claims_no_osi_classifier() -> None:
    """FR-003, FR-006 — every ``License ::`` classifier asserts OSI approval.

    Adding one would make, in machine-readable metadata that indexers and licence
    scanners actually read, precisely the claim FR-006 forbids in prose.
    """
    for rel in PACKAGING_FILES:
        data = tomllib.loads(_read(rel))
        classifiers = data.get("project", {}).get("classifiers", []) or []
        offenders = [c for c in classifiers if c.startswith("License ::")]
        assert not offenders, f"{rel} must carry no 'License ::' classifier, got {offenders}"


def test_in_tree_first_party_package_carries_no_separate_license() -> None:
    """FR-004 — service_inference is covered by the root license, not its own."""
    pkg = REPO_ROOT / "packages" / "service_inference"
    assert pkg.is_dir(), "expected packages/service_inference to exist"

    strays = [
        path.name
        for path in pkg.iterdir()
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING"))
    ]
    assert not strays, (
        f"packages/service_inference must not carry a separate license file, found {strays}"
    )


# ---------------------------------------------------------------------------
# US3 — a security researcher can report privately
# ---------------------------------------------------------------------------


def test_security_md_states_no_support_commitment() -> None:
    """FR-014 — a support posture, not a support commitment.

    "Fixes land on version X" is itself a commitment, so a supported-versions
    table is the same trap in a smaller font.
    """
    text = _read("SECURITY.md").lower()

    assert "support commitment" in text, "SECURITY.md must state its support posture in those terms"
    assert re.search(r"\bno release\b", text), (
        "SECURITY.md must state plainly that no release carries a support commitment, "
        "including the most recent one"
    )
    assert "main" in text, "SECURITY.md must say fixes land on main"


def test_security_md_points_at_private_vulnerability_reporting() -> None:
    """FR-015 — GitHub private advisories are the only route."""
    text = _read("SECURITY.md")
    lowered = text.lower()

    assert "private vulnerability reporting" in lowered or "security advisor" in lowered, (
        "SECURITY.md must direct reporters to GitHub private vulnerability reporting"
    )
    assert "/security/advisories" in text, (
        "SECURITY.md must link the repository's advisory route directly"
    )


def test_security_md_publishes_no_email_address() -> None:
    """FR-015 — no address is published, so nothing can bounce or attract scanners."""
    text = _read("SECURITY.md")

    addresses = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert not addresses, (
        f"SECURITY.md must publish no email address (resolved 2026-08-27), found {addresses}"
    )
    assert "mailto:" not in text.lower()


def test_security_md_gives_a_no_detail_fallback_for_reporters() -> None:
    """FR-015 — a reporter who cannot use advisories must not disclose in the open."""
    lowered = _read("SECURITY.md").lower()

    assert "issue" in lowered, "SECURITY.md must describe the fallback route"
    assert any(
        phrase in lowered
        for phrase in ("without any detail", "no detail", "without details", "do not include")
    ), (
        "SECURITY.md must tell a reporter using the public fallback to include no "
        "vulnerability detail"
    )


def test_security_md_promises_no_response_timeframe() -> None:
    """FR-014, FR-026 — the channel is real; no schedule is committed.

    Enforced as a negative so a later well-meaning edit cannot quietly reintroduce
    a promise nobody is owed. The generic commitment guard covers the posture set;
    this asserts the positive wording is actually present here.
    """
    text = _read("SECURITY.md")
    lowered = text.lower()

    assert not re.search(r"\b\d+\s*(?:business\s*)?(?:hour|day|week|month)s?\b", text, re.IGNORECASE), (
        "SECURITY.md must commit to no timeframe"
    )
    assert not re.search(r"\bwithin\s+\d+", text, re.IGNORECASE)
    assert "capacity" in lowered or "as we can" in lowered, (
        "SECURITY.md must say reports are read and answered as capacity allows"
    )


def test_security_md_leaves_a_seam_for_spec_144() -> None:
    """FR-017 — spec 144 extends this file rather than restructuring it."""
    text = _read("SECURITY.md")

    assert "144" in text, "SECURITY.md must name spec 144 as the owner of the seam"
    assert any(marker in text.lower() for marker in ("threat model", "trust boundar")), (
        "SECURITY.md must reserve a section for threat-model and trust-boundary pointers"
    )


def test_security_md_notes_the_route_goes_live_at_publication() -> None:
    """R5 — do not advertise a channel that is currently disabled.

    GitHub private vulnerability reporting is available on public repositories
    only, and this repository is private, so the primary route is dormant until
    the visibility flip.
    """
    lowered = _read("SECURITY.md").lower()

    assert "public" in lowered, (
        "SECURITY.md must note that the reporting route becomes available when the "
        "repository is public, so it is not read as advertising a dead channel"
    )


# ---------------------------------------------------------------------------
# US2 — a would-be contributor learns the policy before wasting effort
# ---------------------------------------------------------------------------

EXTERNAL_WORKFLOW = ".github/workflows/external-contributions.yml"

ISSUE_TEMPLATES = (
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/feedback.yml",
)

#: Author associations GitHub computes for people inside the project. The author
#: cannot influence this value, which is why the classification is trustworthy.
INTERNAL_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")

EXISTING_WORKFLOWS = (
    ".github/workflows/main-branch-build.yml",
    ".github/workflows/pr-ci.yml",
)


def _load_workflow(rel: str) -> dict:
    """Parse a workflow, working around YAML 1.1's ``on`` -> ``True`` coercion.

    PyYAML reads the bare key ``on`` as the boolean ``True``, so a naive
    ``data["on"]`` misses the trigger block entirely on every GitHub Actions file.
    """
    import yaml

    data = yaml.safe_load(_read(rel))
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def test_contributing_welcomes_issues_without_promising_work() -> None:
    """FR-008 — issues are welcome and read; no triage or response is promised."""
    text = _read("CONTRIBUTING.md")
    lowered = text.lower()

    for phrase in ("bug", "feature", "feedback", "issue"):
        assert phrase in lowered, f"CONTRIBUTING.md must welcome {phrase} submissions"
    assert "roadmap" in lowered, "CONTRIBUTING.md should say submissions inform the roadmap"
    assert not re.search(r"\b(are|will be)\s+triaged\b", lowered), (
        "CONTRIBUTING.md must not promise triage (FR-008): a promise of work made to "
        "strangers for free ages into a wall of untouched issues"
    )


def test_contributing_states_the_closed_pull_request_policy() -> None:
    """FR-008 — public pull requests are refused, and the reason is given."""
    lowered = _read("CONTRIBUTING.md").lower()

    assert "pull request" in lowered
    assert "not accepted" in lowered or "do not accept" in lowered
    assert "supply chain" in lowered or "supply-chain" in lowered, (
        "CONTRIBUTING.md must name the closed-PR policy as a supply-chain security measure"
    )
    assert "closed" in lowered and "unmerged" in lowered, (
        "CONTRIBUTING.md must say external pull requests are closed unmerged"
    )


def test_contributing_tone_is_unapologetic() -> None:
    """FR-008 — friendly and unapologetic, not sheepish."""
    lowered = _read("CONTRIBUTING.md").lower()

    for word in ("sorry", "unfortunately", "we apologize", "we apologise"):
        assert word not in lowered, (
            f"CONTRIBUTING.md must not apologize for the policy, found {word!r}"
        )


def test_contributing_is_a_policy_not_a_build_howto() -> None:
    """FR-008 — a contribution policy, not developer setup instructions."""
    lowered = _read("CONTRIBUTING.md").lower()

    for marker in ("pytest", "make test", "uv sync", "pip install -e"):
        assert marker not in lowered, (
            f"CONTRIBUTING.md is a policy, not a how-to; found build instruction {marker!r}"
        )


def test_pull_request_template_states_the_policy() -> None:
    """FR-009 — an external author sees the policy before the automation runs."""
    lowered = _read(".github/pull_request_template.md").lower()

    assert "pull request" in lowered
    assert "not accepted" in lowered or "closed" in lowered, (
        "the pull request template must state the closed-contribution policy"
    )


def test_issue_templates_exist_and_parse() -> None:
    """FR-010 — a chooser with bug, feature and feedback forms."""
    import yaml

    for rel in ISSUE_TEMPLATES:
        assert (REPO_ROOT / rel).is_file(), f"missing issue template {rel}"
        data = yaml.safe_load(_read(rel))
        assert data, f"{rel} parsed empty"
        if rel.endswith("config.yml"):
            continue
        assert data.get("name"), f"{rel} must declare a non-empty name"
        assert data.get("description"), f"{rel} must declare a non-empty description"


def test_external_contribution_workflow_uses_pull_request_target() -> None:
    """FR-011, research R4 — a fork ``pull_request`` cannot comment, label or close.

    Fork-originated ``pull_request`` events get a read-only token and no secrets,
    so the job would be unable to act at all.
    """
    data = _load_workflow(EXTERNAL_WORKFLOW)
    triggers = data["on"]

    assert "pull_request_target" in triggers, (
        "the workflow must trigger on pull_request_target; a fork pull_request event "
        "has a read-only token and cannot comment, label or close"
    )
    assert "pull_request" not in triggers, (
        "the workflow must not also trigger on pull_request, which would double-fire"
    )


def test_external_contribution_workflow_never_checks_out_the_fork() -> None:
    """FR-013, research R4 — the single line separating safe from exploitable.

    ``pull_request_target`` runs with a write token in the base-repository
    context. Checking out untrusted head code under that token is the
    privilege-escalation hole the pattern is notorious for.
    """
    data = _load_workflow(EXTERNAL_WORKFLOW)

    # Check actual usage, not mentions. The workflow's own header comment
    # explains why checkout must never be added, and that explanation is worth
    # more than a substring check is worth protecting.
    used = [
        step.get("uses", "")
        for job in (data.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]
    offenders = [u for u in used if "checkout" in u.lower()]
    assert not offenders, (
        "the workflow must never check out the fork head: it holds a write token, and "
        f"checking out untrusted code under it is a privilege-escalation hole. Found {offenders}"
    )

    # And no run step may fetch the head by hand either.
    scripts = "\n".join(
        step.get("run", "")
        for job in (data.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    )
    for forbidden in ("git fetch", "git checkout", "git clone"):
        assert forbidden not in scripts, (
            f"a run step fetches the pull request head via {forbidden!r}, which defeats "
            "the no-checkout rule by another route"
        )


def test_external_contribution_workflow_grants_minimal_permissions() -> None:
    """FR-012 — only what commenting, labelling and closing require."""
    data = _load_workflow(EXTERNAL_WORKFLOW)

    scopes: dict[str, str] = dict(data.get("permissions") or {})
    for job in (data.get("jobs") or {}).values():
        scopes.update(job.get("permissions") or {})

    assert scopes.get("pull-requests") == "write", (
        "the workflow needs pull-requests: write to comment, label and close"
    )
    extra_writes = {
        scope: value
        for scope, value in scopes.items()
        if value == "write" and scope != "pull-requests"
    }
    assert not extra_writes, f"the workflow must grant no other write scope, got {extra_writes}"


def test_external_contribution_workflow_exempts_insiders_and_bots() -> None:
    """FR-012 — maintainers and bots must never be scolded by their own automation."""
    raw = _read(EXTERNAL_WORKFLOW)

    for association in INTERNAL_ASSOCIATIONS:
        assert association in raw, (
            f"the workflow must exempt {association} authors; every first-party change "
            "arrives as a pull request and would otherwise trigger the policy response"
        )
    assert "author_association" in raw, (
        "classification must use author_association, which GitHub computes and the "
        "author cannot influence"
    )
    assert "Bot" in raw, "the workflow must exempt bot authors (e.g. the version-sync job)"


def test_external_contribution_workflow_does_not_close_security_reports() -> None:
    """FR-025 — a security-related submission is escalated, never auto-closed.

    The detection runs over author-controlled text, which is acceptable only
    because its sole power is to *suppress* automation. A false positive costs one
    manual close; a false negative auto-closes a vulnerability report.
    """
    raw = _read(EXTERNAL_WORKFLOW)
    lowered = raw.lower()

    assert "vulnerabilit" in lowered or "security" in lowered, (
        "the workflow must detect security-related submissions"
    )
    assert "is_security" in lowered or "security_related" in lowered, (
        "the security carve-out needs an explicit named condition, so the close step "
        "can be gated on it and the intent is legible to a reader"
    )


def test_external_contribution_workflow_verifies_actual_repo_permission() -> None:
    """FR-012 — author_association alone is not sufficient to identify insiders.

    Regression test for a bug that fired in production on PR #212: a maintainer's
    own pull request was commented on and labelled as an external contribution.

    The cause is that ``author_association`` in the webhook payload UNDER-reports
    when organisation membership is private. The same user came back as MEMBER
    from the REST API and as something else in the event payload, so both the
    job-level ``if:`` and the script's own check let it through.

    It can only under-report, never over-report, so using it to SKIP is still
    safe. Using its absence to conclude "outsider" is not. The authoritative
    question is whether the account actually has write access, which is what
    ``getCollaboratorPermissionLevel`` answers.
    """
    raw = _read(EXTERNAL_WORKFLOW)

    assert "getCollaboratorPermissionLevel" in raw, (
        "the workflow must verify actual repository permission; author_association "
        "under-reports for private organisation members and scolded a maintainer's "
        "own pull request when it was the only check"
    )
    for level in ("admin", "write", "maintain"):
        assert f"'{level}'" in raw, f"write-equivalent permission {level!r} must count as internal"

    # An unexpected API failure must not cause a maintainer to be scolded.
    assert "err.status !== 404" in raw, (
        "a non-404 error from the permission lookup must skip rather than assume "
        "the author is external"
    )


def test_existing_workflows_are_untouched() -> None:
    """SC-009 — this feature must not disturb CI.

    A regression guard rather than a new requirement: it exists so a later edit to
    spec 142 cannot quietly change a required check.
    """
    expected = {
        ".github/workflows/main-branch-build.yml": (
            {"push", "workflow_dispatch"},
            "ubuntu-latest",
        ),
        ".github/workflows/pr-ci.yml": ({"pull_request"}, "ubuntu-latest"),
    }
    # sync-version-to-prs.yml was DELETED by spec 146 (issue #215). It existed
    # solely to re-bump a committed version file across open PRs after each
    # release; deriving the version from git tags at release time removed the
    # file and therefore the need for the workflow. This guard is what flagged
    # the change, which is what it is for.
    guidance = (
        "This is spec 142's SC-009 regression guard, asserting that adding the "
        "external-contributions workflow did not disturb existing CI. If you are "
        "changing this workflow deliberately and for unrelated reasons, update the "
        "expectations here in the same commit; the guard is not a claim that these "
        "workflows may never change."
    )
    for rel, (triggers, runner) in expected.items():
        data = _load_workflow(rel)
        assert set(data["on"]) == triggers, (
            f"{rel} triggers changed to {sorted(data['on'])}, expected {sorted(triggers)}. "
            + guidance
        )
        assert runner in _read(rel), f"{rel} runner target changed. " + guidance


# ---------------------------------------------------------------------------
# US4 foundational — accepted licence set and the distributed-closure walk
# ---------------------------------------------------------------------------
#
# The contract these encode lives in
# specs/142-license-and-legal-posture/contracts/accepted-licenses.md. The audit
# document and this check derive from the same data, so the two cannot disagree.

#: Tier 1 — permitted with no comment required.
TIER1_PERMITTED = frozenset(
    {
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "Unlicense",
        "Zlib",
    },
)

#: Tier 2 — permitted only with a written rationale in the audit.
TIER2_EXCEPTION = frozenset(
    {
        "MPL-2.0",
        "LGPL-2.0",
        "LGPL-2.1",
        "LGPL-3.0",
    },
)

#: Tier 3 — rejected outright. GPL-family terms forbid adding restrictions, and
#: ELv2's hosted-service limitation is exactly such a restriction, so the
#: conflict is real rather than cautious.
TIER3_REJECTED = frozenset(
    {
        "GPL-1.0",
        "GPL-2.0",
        "GPL-3.0",
        "AGPL-1.0",
        "AGPL-3.0",
        "SSPL-1.0",
        "UNKNOWN",
    },
)

#: Real packaging metadata is inconsistent, so historical spellings normalize
#: onto SPDX identifiers before any tier decision is made.
LICENSE_ALIASES = {
    "bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "simplified bsd license": "BSD-2-Clause",
    "mit license": "MIT",
    "the mit license (mit)": "MIT",
    "apache software license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "isc license (iscl)": "ISC",
}

#: Free-text ``License`` values that describe the *arrangement* rather than name
#: a licence. They are short enough to pass the length filter but carry no
#: identifier, so trusting them would classify a package on a string that says
#: nothing. A package whose metadata lands here must be resolved by hand into
#: ``VERIFIED_LICENSES`` below.
UNINFORMATIVE_LICENSE_VALUES = frozenset({"dual license", "see license", "see license file"})

#: Packages whose published metadata cannot classify them, resolved by reading
#: the licence text the package actually ships and recording the result here.
#:
#: **Why this is a hand-verified table and not an inference.** The tempting rule
#: is "multiple ``License ::`` classifiers means dual-licensed, so OR them
#: together", and ``python-dateutil`` would come out right. ``orjson`` proves the
#: rule unsound: it publishes ``Apache``, ``MIT`` *and* ``MPL-2.0`` classifiers,
#: yet its real expression is ``MPL-2.0 AND (Apache-2.0 OR MIT)`` — Tier 2. OR-ing
#: its classifiers would silently promote it to Tier 1 and drop the MPL
#: obligation. Since ``OR`` resolves to the most permissive operand, a wrong
#: guess here fails open, which is the direction that must never be guessed.
#:
#: Each entry records how it was verified, so the claim can be re-checked rather
#: than taken on trust.
VERIFIED_LICENSES = {
    # Metadata says only "Dual License". Its LICENSE file carries the Apache-2.0
    # text and the 3-clause BSD text in full, and its classifiers name both.
    # Verified 2026-08-28 against python_dateutil-2.9.0.post0.dist-info/LICENSE.
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause",
}

#: Bare family names that appear in real metadata without a variant. Every
#: member of these families is permissive and compatible with ELv2 distribution,
#: so the tier decision is safe without pinning the variant. Recording them as a
#: family rather than aliasing them to a specific SPDX id keeps the check honest:
#: ``xxhash`` really does say only "BSD", and claiming to know it is 3-Clause
#: would be inventing precision the upstream metadata does not have. The audit
#: notes the imprecision per package.
PERMISSIVE_FAMILIES = frozenset({"BSD"})

#: First-party packages are not judged against the tiers. They are asserted to
#: declare the ELv2 reference instead (FR-003, FR-004). All three reported
#: UNKNOWN before this feature, which is the gap FR-003 closes and the reason
#: this exemption must exist rather than simply rejecting UNKNOWN everywhere.
FIRST_PARTY = frozenset(
    # Our own packages in this repository. They carry no third-party
    # distribution obligation: they are covered by the repo-root LICENSE and
    # NOTICE, which is why each declares LicenseRef-Elastic-License-2.0
    # rather than an OSI identifier. 339 added coordinare-ci-detection, the shared
    # CI/test-command detection the performer image installs.
    {"coordinare", "performer", "coordinare-service-inference", "coordinare-ci-detection"},
)

#: ``(lock file, root package)`` for each project coordinare distributes.
DISTRIBUTED_PROJECTS = (
    ("uv.lock", "coordinare"),
    ("agent/performer/uv.lock", "performer"),
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distributed_closure(lock_rel: str, root_name: str) -> dict[str, bool]:
    """Transitive runtime closure of ``root_name``, mapped to unconditional-ness.

    Returns ``{package: reachable_unconditionally}``. Walks only the
    ``dependencies`` list of each locked package: optional and development groups
    are excluded on purpose, since development dependencies are never conveyed to
    a recipient and including them would misreport what coordinare actually ships.

    A package is *conditional* when every path to it passes through a
    marker-gated edge (``psycopg -> tzdata`` under ``sys_platform == 'win32'``,
    for instance). Conditional packages may legitimately be absent from this
    environment, so they cannot be treated as installation failures, but they are
    tracked separately rather than dropped.
    """
    locked = tomllib.loads(_read(lock_rel))
    packages = {_canonical(p["name"]): p for p in locked["package"]}

    root = _canonical(root_name)
    reachable: dict[str, bool] = {}
    stack: list[tuple[str, bool]] = [(root, True)]
    while stack:
        name, unconditional = stack.pop()
        if name not in packages:
            continue
        # Revisit only when a better (unconditional) path is discovered.
        if name in reachable and reachable[name] >= unconditional:
            continue
        reachable[name] = unconditional
        for dep in packages[name].get("dependencies", []):
            stack.append((_canonical(dep["name"]), unconditional and "marker" not in dep))
    reachable.pop(root, None)
    return reachable


def _resolve_license(dist_name: str) -> tuple[str, str]:
    """``(license_id, source_field)`` for an installed distribution.

    Precedence follows how reliable each field actually is:
    A hand-verified entry is a **fallback, never an override**. Consulting it
    first would be fail-open: a package that later publishes a real
    ``License-Expression`` — including a rejected one after a relicensing — would
    have it masked by a note somebody wrote once, and the gate would keep passing
    a dependency it should now reject. So the authoritative fields win, and the
    table only answers where they say nothing usable.

    ``License-Expression`` (PEP 639 SPDX) beats a short free-text ``License``,
    which beats a ``License ::`` classifier. Long free-text values are rejected
    because in practice they are the entire licence text pasted into a header,
    not an identifier; short ones that name an arrangement rather than a licence
    ("Dual License") are refused for the same reason and fall through.
    """
    from importlib import metadata

    meta = metadata.metadata(dist_name)

    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression, "License-Expression"

    free_text = (meta.get("License") or "").strip()
    if (
        free_text
        and len(free_text) <= 40
        and "\n" not in free_text
        and free_text.lower() not in UNINFORMATIVE_LICENSE_VALUES
    ):
        return free_text, "License"

    if dist_name in VERIFIED_LICENSES:
        return VERIFIED_LICENSES[dist_name], "verified"

    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip(), "Classifier"

    return "UNKNOWN", "none"


#: Severity ordering. ``AND`` takes the maximum (every operand's obligations
#: apply); ``OR`` takes the minimum (the recipient may elect the easiest term).
_SEVERITY = {"permitted": 0, "exception": 1, "rejected": 2}
_BY_SEVERITY = {v: k for k, v in _SEVERITY.items()}


def _tier_of_atom(token: str) -> str:
    """Tier of a single licence identifier, aliases and suffixes normalized."""
    normalized = LICENSE_ALIASES.get(token.strip().lower(), token.strip())
    base = re.sub(r"-(?:only|or-later)$", "", normalized)
    if base in TIER1_PERMITTED or base in PERMISSIVE_FAMILIES:
        return "permitted"
    if base in TIER2_EXCEPTION:
        return "exception"
    return "rejected"


def _tier_of(license_id: str) -> str:
    """``permitted`` / ``exception`` / ``rejected`` for an SPDX-style expression.

    Evaluates ``AND`` and ``OR`` with their real SPDX semantics rather than
    scanning for a single acceptable token:

    * ``OR`` is disjunctive. The recipient elects one term, so the expression is
      as permissive as its easiest alternative. ``Apache-2.0 OR MIT`` is Tier 1.
    * ``AND`` is conjunctive. Every operand's obligations apply simultaneously,
      so the expression is as restrictive as its hardest operand.
      ``MPL-2.0 AND (Apache-2.0 OR MIT)`` (which is what ``orjson`` actually
      publishes) is Tier 2, because the MPL-2.0 obligation is real regardless of
      the permissive half.

    Getting this backwards is not cosmetic: treating any permissive token as
    sufficient would let ``GPL-3.0 AND MIT`` through, which is precisely the
    class of dependency this check exists to reject. ``AND`` binds tighter than
    ``OR``, per SPDX.
    """
    tokens = re.findall(r"\(|\)|[^\s()]+", license_id.strip())
    if not tokens:
        return "rejected"

    pos = 0

    def parse_or() -> int:
        nonlocal pos
        severity = parse_and()
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            pos += 1
            severity = min(severity, parse_and())
        return severity

    def parse_and() -> int:
        nonlocal pos
        severity = parse_atom()
        while pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
            severity = max(severity, parse_atom())
        return severity

    def parse_atom() -> int:
        nonlocal pos
        if pos >= len(tokens):
            return _SEVERITY["rejected"]
        token = tokens[pos]
        if token == "(":
            pos += 1
            severity = parse_or()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return severity
        pos += 1
        # Multi-word free-text spellings ("BSD License") arrive as separate
        # tokens; rejoin any run of non-operator words before classifying.
        words = [token]
        while (
            pos < len(tokens)
            and tokens[pos] not in ("(", ")")
            and tokens[pos].upper() not in ("AND", "OR")
        ):
            words.append(tokens[pos])
            pos += 1
        return _SEVERITY[_tier_of_atom(" ".join(words))]

    return _BY_SEVERITY[parse_or()]


def test_distributed_closure_walks_both_locks() -> None:
    """T003 — the closure derivation works and excludes dev-only packages."""
    for lock_rel, root in DISTRIBUTED_PROJECTS:
        closure = _distributed_closure(lock_rel, root)
        assert closure, f"{lock_rel}: closure from {root!r} came back empty"

        total = len(tomllib.loads(_read(lock_rel))["package"])
        assert len(closure) < total, (
            f"{lock_rel}: closure ({len(closure)}) should exclude dev-only packages "
            f"from the {total} locked"
        )

    # The marker walk must actually distinguish conditional edges, or the
    # unconditional-absence check below would silently never fire.
    root_closure = _distributed_closure("uv.lock", "coordinare")
    assert any(not unconditional for unconditional in root_closure.values()), (
        "expected at least one marker-gated dependency (psycopg pulls tzdata on win32); "
        "if this fails the marker walk has stopped working and conditional packages "
        "would be misreported as installation failures"
    )


def test_every_distributed_dependency_carries_an_accepted_license() -> None:
    """FR-019, FR-020 — the allowlist, enforced on every distributed package.

    Failure names the package and its licence (SC-007) so the fix is obvious
    without re-deriving anything.
    """
    from importlib import metadata

    rejected: list[str] = []
    absent_unconditional: list[str] = []
    absent_conditional: list[str] = []

    for lock_rel, root in DISTRIBUTED_PROJECTS:
        for name, unconditional in sorted(_distributed_closure(lock_rel, root).items()):
            if name in FIRST_PARTY:
                continue
            try:
                license_id, source = _resolve_license(name)
            except metadata.PackageNotFoundError:
                target = absent_unconditional if unconditional else absent_conditional
                target.append(f"{name} (from {lock_rel})")
                continue
            if _tier_of(license_id) == "rejected":
                rejected.append(f"{name} == {license_id!r} (via {source}, from {lock_rel})")

    assert not rejected, (
        "distributed dependencies carry licences outside the accepted set "
        "(see specs/142-license-and-legal-posture/contracts/accepted-licenses.md):\n  "
        + "\n  ".join(rejected)
    )
    # An unconditionally-reachable package with no installed metadata is a hard
    # failure, not a skip: a silent skip is how an unlicensed package eventually
    # ships.
    assert not absent_unconditional, (
        "distributed dependencies have no resolvable licence metadata:\n  "
        + "\n  ".join(absent_unconditional)
    )
    # Marker-gated packages may legitimately be absent here. They are NOT
    # verified by this check, which is a real and acknowledged limitation: a
    # platform-conditional dependency under a rejected licence would not be
    # caught in this environment. The audit records each one explicitly so the
    # gap is visible rather than implied.
    if absent_conditional:
        recorded = _read("specs/142-license-and-legal-posture/dependency-license-audit.md")
        unrecorded = [entry for entry in absent_conditional if entry.split(" ")[0] not in recorded]
        assert not unrecorded, (
            "platform-conditional dependencies are not verifiable in this environment "
            "and must therefore be listed explicitly in the audit, so the gap is "
            "visible:\n  " + "\n  ".join(unrecorded)
        )


def test_first_party_packages_are_exempt_but_declare_elv2() -> None:
    """Contract "First-party exemption" — exempt from tiers, not from declaring."""
    for rel in PACKAGING_FILES:
        data = tomllib.loads(_read(rel))
        name = _canonical(data["project"]["name"])
        assert name in {_canonical(n) for n in FIRST_PARTY}, (
            f"{rel} declares {name!r}, which is not in the first-party exemption set; "
            "either the set is stale or this package should be judged against the tiers"
        )
        assert data["project"].get("license") == ELV2_LICENSE_ID


def test_tier_classification_handles_real_world_expressions() -> None:
    """FR-019 — the classifier must cope with the metadata actually in the tree."""
    assert _tier_of("MIT") == "permitted"
    assert _tier_of("BSD License") == "permitted"
    assert _tier_of("Apache Software License") == "permitted"
    assert _tier_of("Apache-2.0 OR MIT") == "permitted"

    # A bare family name with no variant, as xxhash actually publishes.
    assert _tier_of("BSD") == "permitted"

    # OR is disjunctive: as permissive as the easiest alternative.
    assert _tier_of("Apache-2.0 OR BSD-3-Clause") == "permitted"

    # AND is conjunctive: as restrictive as the hardest operand. This is what
    # orjson actually publishes, and the MPL-2.0 half still binds.
    assert _tier_of("MPL-2.0 AND (Apache-2.0 OR MIT)") == "exception"

    # The bug this evaluator exists to prevent: a permissive token must NOT
    # launder a conjunctive GPL obligation.
    assert _tier_of("GPL-3.0 AND MIT") == "rejected"
    assert _tier_of("MIT AND AGPL-3.0") == "rejected"

    # But a genuine choice that includes a permissive option is fine.
    assert _tier_of("GPL-3.0 OR MIT") == "permitted"

    # Weak copyleft alone requires a rationale.
    assert _tier_of("MPL-2.0") == "exception"
    assert _tier_of("LGPL-3.0-only") == "exception"
    assert _tier_of("LGPL-2.1-or-later") == "exception"

    # Strong copyleft and missing metadata are refused.
    assert _tier_of("GPL-3.0-or-later") == "rejected"
    assert _tier_of("AGPL-3.0") == "rejected"
    assert _tier_of("UNKNOWN") == "rejected"


# ---------------------------------------------------------------------------
# US4 — recorded evidence for going public
# ---------------------------------------------------------------------------

AUDIT = "specs/142-license-and-legal-posture/dependency-license-audit.md"
SCRUB = "specs/142-license-and-legal-posture/pre-public-scrub.md"


def test_uninformative_license_metadata_is_not_trusted() -> None:
    """A ``License`` value naming an arrangement is not an identifier.

    ``python-dateutil`` publishes ``License: Dual License``, which is short
    enough to pass the length filter and says nothing. Classifying on it would
    reject a permissively-licensed package; classifying by OR-ing its
    classifiers would be a guess that ``orjson`` disproves. It is resolved by
    hand instead, and this pins that.
    """
    assert "dual license" in UNINFORMATIVE_LICENSE_VALUES
    license_id, source = _resolve_license("python-dateutil")
    assert source == "verified", "an uninformative License value must not be trusted"
    assert _tier_of(license_id) == "permitted"


def test_verified_licenses_is_a_fallback_and_never_overrides_real_metadata() -> None:
    """The hand table must not mask what a package actually publishes.

    Consulting it first would be fail-open in the one direction that matters: a
    package that relicenses and publishes a rejected ``License-Expression`` would
    keep passing the gate on the strength of a note written before the change.

    The second assertion keeps the table from outliving its reason to exist. An
    entry is only justified while the package's own metadata still cannot
    classify it; once upstream publishes something usable, the entry is stale and
    must go, or it becomes an unreviewed override in waiting.
    """
    from importlib import metadata

    for dist_name, recorded in VERIFIED_LICENSES.items():
        meta = metadata.metadata(dist_name)
        expression = (meta.get("License-Expression") or "").strip()
        assert not expression, (
            f"{dist_name} now publishes License-Expression {expression!r}; drop its "
            "VERIFIED_LICENSES entry so the authoritative field is what the gate reads"
        )
        free_text = (meta.get("License") or "").strip()
        assert free_text.lower() in UNINFORMATIVE_LICENSE_VALUES or len(free_text) > 40, (
            f"{dist_name} publishes a usable License value {free_text!r}; its "
            "VERIFIED_LICENSES entry is stale and would now be masking real metadata"
        )
        assert _tier_of(recorded) != "rejected"


def test_multiple_license_classifiers_are_never_or_joined() -> None:
    """The unsound shortcut this table exists to avoid, pinned by its counterexample.

    ``orjson`` carries Apache, MIT *and* MPL-2.0 classifiers while its true
    expression is ``MPL-2.0 AND (Apache-2.0 OR MIT)``. Because ``OR`` resolves to
    the most permissive operand, inferring an expression from classifiers fails
    *open* — it would drop orjson's MPL obligation and promote it out of Tier 2.
    """
    assert _tier_of("MPL-2.0 AND (Apache-2.0 OR MIT)") == "exception"
    assert _tier_of("Apache-2.0 OR MIT OR MPL-2.0") == "permitted"
    assert _resolve_license("orjson")[1] != "Classifier"


def test_audit_is_dated_and_covers_the_whole_closure() -> None:
    """FR-018, SC-006 — every distributed package appears, with a date."""
    from importlib import metadata

    text = _read(AUDIT)
    assert re.search(r"\b20\d\d-\d\d-\d\d\b", text), "the audit must carry the date it was taken"

    expected: set[str] = set()
    for lock_rel, root in DISTRIBUTED_PROJECTS:
        for name in _distributed_closure(lock_rel, root):
            if name in FIRST_PARTY:
                continue
            # A conditional package that is absent here is still expected in the
            # audit; its own assertion covers the licence question.
            with contextlib.suppress(metadata.PackageNotFoundError):
                metadata.metadata(name)
            expected.add(name)

    missing = sorted(name for name in expected if f"`{name}`" not in text)
    assert not missing, (
        f"the audit omits {len(missing)} distributed packages: {missing[:10]}"
        " — regenerate it rather than editing by hand, since the generator and the "
        "check share their derivation"
    )


def test_audit_gives_every_exception_a_rationale_and_a_consumption_mode() -> None:
    """FR-019, FR-019a — an exception without a rationale fails.

    This is what stops the exception mechanism decaying into a second allowlist
    nobody has thought about. Adding a weak-copyleft package stays possible; it
    costs a sentence explaining why it is acceptable.
    """
    text = _read(AUDIT)

    assert "RATIONALE MISSING" not in text, "a Tier 2 package has no written rationale in the audit"

    from importlib import metadata

    for lock_rel, root in DISTRIBUTED_PROJECTS:
        for name in sorted(_distributed_closure(lock_rel, root)):
            if name in FIRST_PARTY:
                continue
            try:
                license_id, _ = _resolve_license(name)
            except metadata.PackageNotFoundError:
                continue
            if _tier_of(license_id) != "exception":
                continue
            section = re.search(
                rf"^### `{re.escape(name)}`.*?(?=^### |\Z)", text, re.MULTILINE | re.DOTALL,
            )
            assert section, f"Tier 2 package {name!r} has no section in the audit"
            body = section.group(0)
            assert "**Rationale**" in body and len(body) > 200, (
                f"Tier 2 package {name!r} needs a substantive rationale"
            )
            assert "**Consumed as**" in body, (
                f"Tier 2 package {name!r} must record how it is consumed (FR-019a): "
                "an imported library and a separately invoked executable sit in "
                "genuinely different positions"
            )


def test_audit_records_the_image_only_redistributed_package() -> None:
    """R6 — semgrep is redistributed in the image but is not a Python dependency."""
    text = _read(AUDIT)

    assert "`semgrep`" in text, (
        "the audit must cover semgrep: it is absent from both lock closures because it "
        "is not a Python dependency, but it is genuinely redistributed as performer "
        "image content"
    )
    assert "subprocess" in text, "the audit must record that semgrep is invoked as a subprocess"
    assert "aggregation" in text.lower(), (
        "the audit should state why a separately invoked executable is not a combined work"
    )


def test_audit_states_its_own_limitations() -> None:
    """FR-018 — a snapshot that does not admit it is a snapshot is misleading."""
    lowered = _read(AUDIT).lower()

    assert "point-in-time" in lowered or "snapshot" in lowered, (
        "the audit must state that it is a point-in-time snapshot"
    )
    assert "self-reported" in lowered or "misdeclare" in lowered, (
        "the audit must acknowledge that licence metadata is self-reported"
    )


def test_scrub_checklist_items_are_concrete_and_recorded() -> None:
    """FR-021, FR-023, SC-008 — every item names a check and records an outcome."""
    text = _read(SCRUB)

    assert "| Scope |" in text or "**Scope**" in text, "scrub items must record a scope"
    assert "Outcome" in text, "scrub items must record an outcome"
    assert "`" in text, "scrub items must name concrete commands, not intentions"

    unrecorded = re.findall(
        r"^\|\s*`[^`]+`\s*\|[^|]*\|[^|]*\|\s*(?:TODO|TBD|\s*)\|", text, re.MULTILINE,
    )
    assert not unrecorded, f"scrub items with no recorded outcome: {unrecorded}"


def test_scrub_marks_at_flip_items_and_excludes_them_from_done() -> None:
    """R5, FR-016, SC-008 — a fully ticked list must not imply a false readiness."""
    text = _read(SCRUB)
    lowered = text.lower()

    assert "at_flip" in lowered, "the scrub must mark items that can only be done at the flip"
    assert "private vulnerability reporting" in lowered, (
        "enabling private vulnerability reporting must appear as an at_flip item: it is "
        "public-repository-only and this repository is private (FR-016)"
    )
    assert "ui" in lowered, (
        "the scrub must say the enablement needs the GitHub web UI, since the available "
        "tokens cannot write repository administration settings"
    )


def test_scrub_attributes_de_sparking_to_spec_145() -> None:
    """FR-022 — this feature owns the checklist, spec 145 owns the cleanup."""
    text = _read(SCRUB)

    assert "145" in text, "the scrub must attribute internal-reference removal to spec 145"
    assert "199" in text, "the scrub should cite issue #199 as the owning ticket"
