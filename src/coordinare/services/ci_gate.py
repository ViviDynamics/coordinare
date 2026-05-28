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

    def signature(self) -> str:
        """Stable 16-char dedup signature per contracts §1."""
        return compute_ci_gate_signature(
            head_sha=self.head_sha,
            verdict=self.verdict,
            required_checks=list(self.required_checks),
            failed_names=[c.name for c in self.failed_checks],
        )


__all__ = ["CIGateDecision", "FailedCheck", "ResolverSource", "Verdict", "compute_ci_gate_signature"]
