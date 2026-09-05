"""Scenario fixture model and manifest loader (spec 164 T043).

A scenario is a small repository with a base commit, a head commit, acceptance
criteria, and an expected verdict CLASS. Assertions are qualitative (FR-019):
model output is nondeterministic, so exact strings are never asserted. What is
asserted is that the verdict class is right, that a failure names the correct
artifact, and that a healthy change is never failed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ExpectedVerdict = Literal["pass", "fail", "environment_error"]


class ScenarioFixture(BaseModel):
    __test__ = False  # not a pytest class

    name: str
    summary: str = ""
    base_files: dict[str, str] = Field(default_factory=dict)
    head_files: dict[str, str] = Field(default_factory=dict)
    criteria: list[str] = Field(default_factory=list)
    claimed_change: str = ""
    expected_verdict: ExpectedVerdict
    #: Substrings the failure must reference. This is the "right answer for the
    #: RIGHT REASON" assertion -- a scenario that fails for an unrelated reason
    #: is not a pass.
    must_name: list[str] = Field(default_factory=list)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_all() -> list[ScenarioFixture]:
    return [load(p) for p in sorted(FIXTURE_DIR.glob("*.yaml"))]


def load(path: Path) -> ScenarioFixture:
    return ScenarioFixture.model_validate(yaml.safe_load(path.read_text()))
