"""CI gate decision model (spec 075).

The gate evaluates required checks at the implementer→reviewer boundary and
returns a `CIGateDecision`.  Shape is pinned by
`specs/075-implementer-ci-gate/contracts/gate-decision.md` and exercised by
`tests/contract/test_gate_decision_schema.py`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Verdict = Literal["pass", "hold", "bounce", "escalate"]
ResolverSource = Literal["persona_check_map", "branch_protection", "all_head_checks"]
SignatureComparison = Literal["match", "distinct", "collision"]

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def compute_ci_gate_signature(
    head_sha: str,
    verdict: str,
    required_checks: list[str],
    failed_names: list[str],
) -> str:
    """Stable 16-char dedup signature for a CI-gate decision (contracts §1).

    Extracted as a module-level function so both ``CIGateDecision.signature()``
    and the dict-form helper in ``notify._ci_gate_signature()`` share a single
    implementation — preventing silent divergence if the algorithm changes.
    """
    payload = (
        f"{head_sha}|{verdict}|"
        f"{','.join(required_checks)}|"
        f"{','.join(failed_names)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class FailedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    conclusion: str
    html_url: str | None = None
    last_log_line: str | None = None

    @field_validator("last_log_line")
    @classmethod
    def _truncate_log_line(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v[:200]


class FailedCheckWithSignature(FailedCheck):
    """A failing HEAD check carrying its 090-L2 failure signature(s).

    Subclass of ``FailedCheck`` (``extra="forbid"`` and the ``last_log_line``
    truncation are inherited) so a signature-bearing failure serializes
    identically to a plain ``FailedCheck`` plus exactly two fields
    (``head_signature`` / ``baseline_signature``).  This keeps a decision
    produced with L2 disabled byte-identical to a pre-spec-090 one (SC-006):
    the classification lists are simply empty.

    - ``head_signature`` — the 16-char signature of the HEAD failure
      (``failure_signature.make_failure_signature``), always present.
    - ``baseline_signature`` — the signature of the same-named baseline
      failure, or ``None`` when the base branch has no such failure.
    """

    head_signature: str
    baseline_signature: str | None = None


def compare_signatures(
    head_signature: str,
    head_reason: str,
    baseline_signature: str,
    baseline_reason: str,
) -> SignatureComparison:
    """Compare a HEAD and baseline failure signature with collision detection.

    The signatures are 16-char truncations of a sha256 (``make_failure_signature``).
    Truncation admits astronomically-rare collisions: two genuinely *different*
    normalized reasons can hash to the same 16-char value.  Such a collision must
    never masquerade as an inherited match (anti-masking, FR-009), so the
    comparator also takes the normalized reasons and returns three-valued:

    - ``"distinct"``  — the hashes differ → genuinely different reason
      (INTRODUCED).  Decided first; the reasons are not consulted, so an
      inconsistent (equal-reason / different-hash) pair still reads ``distinct``.
    - ``"match"``     — equal hash **and** equal reason → INHERITED-eligible.
    - ``"collision"`` — equal hash but **different** reason → route to UNKNOWN,
      never INHERITED (data-model §1 collision handling, §5 row 2).
    """
    if head_signature != baseline_signature:
        return "distinct"
    if head_reason == baseline_reason:
        return "match"
    return "collision"


class CIGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    head_sha: str
    required_checks: list[str] = Field(default_factory=list)
    failed_checks: list[FailedCheck] = Field(default_factory=list)
    pending_checks: list[str] = Field(default_factory=list)
    resolver_source: ResolverSource
    bounce_count_after: int = Field(ge=0)
    max_bounces_per_head: int = Field(default=0, ge=0)
    decided_at: str

    # 090-L2 classification (observe-only).  All default empty so a decision
    # produced with the classification gate disabled is byte-identical to a
    # pre-spec-090 one (SC-006) — these lists carry no influence over `verdict`,
    # `signature()`, or any routing decision.  ``inherited``/``introduced`` carry
    # signatures; ``flake``/``unknown`` are plain failures (no signature needed).
    inherited_checks: list[FailedCheckWithSignature] = Field(default_factory=list)
    introduced_checks: list[FailedCheckWithSignature] = Field(default_factory=list)
    flake_checks: list[FailedCheck] = Field(default_factory=list)
    unknown_checks: list[FailedCheck] = Field(default_factory=list)

    @field_validator("head_sha")
    @classmethod
    def _validate_sha(cls, v: str) -> str:
        # Empty SHA is allowed for the "no PR / no head_sha" early-return PASS
        # decision (FR-013); otherwise require a full 40-char lowercase hex.
        if v == "":
            return v
        if not _SHA40_RE.match(v):
            raise ValueError(f"head_sha must be empty or 40-char lowercase hex, got {v!r}")
        return v

    @field_validator("required_checks")
    @classmethod
    def _validate_required_sorted(cls, v: list[str]) -> list[str]:
        if list(v) != sorted(v):
            raise ValueError("required_checks must be sorted ASCII ascending")
        return v

    @model_validator(mode="after")
    def _validate_verdict_invariants(self) -> CIGateDecision:
        if self.verdict in ("pass", "hold") and self.failed_checks:
            raise ValueError(f"failed_checks must be empty when verdict={self.verdict!r}")
        if self.verdict in ("bounce", "escalate") and not self.failed_checks:
            raise ValueError(f"failed_checks must be non-empty when verdict={self.verdict!r}")
        if self.verdict in ("bounce", "escalate") and not self.required_checks:
            raise ValueError(
                f"required_checks must be non-empty when verdict={self.verdict!r}"
            )
        if self.verdict != "hold" and self.pending_checks:
            raise ValueError(f"pending_checks must be empty when verdict={self.verdict!r}")
        if self.verdict in ("pass", "hold") and self.bounce_count_after != 0:
            raise ValueError(f"bounce_count_after must be 0 when verdict={self.verdict!r}")
        return self

    @model_validator(mode="after")
    def _validate_classification_invariants(self) -> CIGateDecision:
        """Observe-only exactly-one-classification guard (090-L2, SC-006).

        Deliberately separate from ``_validate_verdict_invariants`` so the
        verdict path is provably untouched: this validator never reads or
        mutates ``verdict``.  When all four classification lists are empty it is
        a strict no-op, so every pre-spec-090 decision stays valid and a
        decision produced with the classification gate disabled is byte-identical
        to the baseline (FR-013, FR-014, SC-006).  Once any list is populated it
        becomes a completeness guard: every failing check must be classified by
        exactly one list, and no list may name a check that isn't failing.
        """
        classified_names = [
            c.name
            for c in (
                *self.inherited_checks,
                *self.introduced_checks,
                *self.flake_checks,
                *self.unknown_checks,
            )
        ]
        if not classified_names:
            return self

        failed_names = {c.name for c in self.failed_checks}

        for name in classified_names:
            if name not in failed_names:
                raise ValueError(
                    f"classification names check {name!r} not present in failed_checks"
                )

        if len(classified_names) != len(set(classified_names)):
            seen: set[str] = set()
            dupes = sorted({n for n in classified_names if n in seen or seen.add(n)})
            raise ValueError(
                f"checks classified in more than one list: {dupes}"
            )

        unclassified = sorted(failed_names - set(classified_names))
        if unclassified:
            raise ValueError(
                f"failed_checks not classified into exactly one list: {unclassified}"
            )
        return self

    def signature(self) -> str:
        """Stable 16-char dedup signature per contracts §1."""
        return compute_ci_gate_signature(
            head_sha=self.head_sha,
            verdict=self.verdict,
            required_checks=list(self.required_checks),
            failed_names=[c.name for c in self.failed_checks],
        )


__all__ = [
    "CIGateDecision",
    "FailedCheck",
    "FailedCheckWithSignature",
    "ResolverSource",
    "SignatureComparison",
    "Verdict",
    "compare_signatures",
    "compute_ci_gate_signature",
]
