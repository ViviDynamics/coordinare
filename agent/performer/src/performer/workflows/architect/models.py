"""The blueprint: the architect workflow's single output.

Every list and string is bounded (spec 165 FR-005, data-model.md) so the
dispatch payload has a known maximum size and a runaway model cannot produce a
plan nobody can act on. ``size`` is NOT a field the model sets: code derives it
after validation (size.py), so ``extra="forbid"`` rejects a model that tries.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ScopeEntry = Annotated[str, StringConstraints(max_length=160)]
Risk = Annotated[str, StringConstraints(max_length=300)]


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Milestone(_Bounded):
    goal: str = Field(..., max_length=200)
    scope: list[ScopeEntry] = Field(..., max_length=8)
    done_when: str = Field(..., max_length=300)


class Module(_Bounded):
    path: str = Field(..., max_length=120)
    note: str = Field(..., max_length=200)


class DataModelChange(_Bounded):
    kind: Literal["table", "model", "column", "index", "migration"]
    name: str = Field(..., max_length=120)
    note: str = Field(..., max_length=200)


class DataModel(_Bounded):
    changes: list[DataModelChange] = Field(..., max_length=12)


class Interface(_Bounded):
    name: str = Field(..., max_length=120)
    kind: Literal["endpoint", "class", "event", "cli"]
    contract: str = Field(..., max_length=300)


class Criterion(_Bounded):
    """One testable acceptance criterion: the verification brief's unit."""

    surface: str = Field(..., max_length=120)
    action: str = Field(..., max_length=200)
    expected: str = Field(..., max_length=300)
    kind: Literal["functional", "visual", "command"]


class DocTopic(_Bounded):
    """One thing the documenter must write, and where."""

    topic: str = Field(..., max_length=120)
    location: str = Field(..., max_length=160)
    say: str = Field(..., max_length=400)


class Blueprint(_Bounded):
    summary: str = Field(..., max_length=600)
    milestones: list[Milestone] = Field(..., min_length=1, max_length=7)
    # Every top-level field is required, empty lists included: an empty docs
    # list is a decision ("nothing to document"), not an omission.
    modules: list[Module] = Field(..., max_length=12)
    data_model: DataModel
    interfaces: list[Interface] = Field(..., max_length=12)
    risks: list[Risk] = Field(..., max_length=8)
    criteria: list[Criterion] = Field(..., min_length=1, max_length=12)
    docs: list[DocTopic] = Field(..., max_length=8)


__all__ = [
    "Blueprint",
    "Criterion",
    "DataModel",
    "DataModelChange",
    "DocTopic",
    "Interface",
    "Milestone",
    "Module",
]
