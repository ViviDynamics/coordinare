"""One glossary for every internal code the dashboard shows (#136).

Before this module the dashboard had no label dictionary: ``humanPhase`` in
``helpers.js`` replaced underscores with spaces and title-cased whatever code
it was handed. Labels were therefore an accident of the code's spelling, and
the same concept could carry two or three names.

This is the single source of display labels. The dashboard reads it (it is
served as ``/static/vocabulary.js`` and as ``/api/vocabulary``), and a guard
test fails when a conductor phase, card stage, agent role or outcome exists
in the code without an entry here.

The labels below are deliberately the ones the dashboard showed before this
module existed, so introducing it changes no text. Renames land one concept
at a time in the naming issues that build on it (#137 to #144).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Term:
    """One glossary entry: an internal code and how people read it."""

    code: str
    label: str
    plural: str
    description: str


def _terms(*rows: tuple[str, str, str, str]) -> dict[str, Term]:
    return {code: Term(code, label, plural, description) for code, label, plural, description in rows}


#: What the conductor itself is doing (``graph.state.DaemonPhase``).
PHASES: dict[str, Term] = _terms(
    ("idle", "Idle", "Idle", "The conductor has nothing to do for this card until the next check."),
    ("dispatching", "Dispatching", "Dispatching", "The conductor is starting an agent for the card's next stage."),
    ("monitoring_agent", "Monitoring Agent", "Monitoring Agent", "The conductor is watching a running agent."),
    ("monitoring_performer", "Monitoring Performer", "Monitoring Performer", "The conductor is watching a running agent."),
    ("monitoring_pr", "Monitoring Pr", "Monitoring Pr", "The card's pull request is waiting on checks or a human review."),
    ("merging", "Merging", "Merging", "The conductor is merging the card's pull request."),
    ("relay_feedback", "Relay Feedback", "Relay Feedback", "The conductor is passing review feedback back to an agent."),
    ("blocked", "Blocked", "Blocked", "The card is stopped until a person acts."),
    ("recovery", "Recovery", "Recovery", "The conductor is recovering this card after a restart."),
    ("system_error", "System Error", "System Errors", "The conductor hit an error on this card and will retry within its budget."),
    ("active", "Active", "Active", "A session whose exact state is not known yet."),
)

#: Where a card is in its pipeline (the values of ``lifecycle.ROLE_TO_STAGE``).
STAGES: dict[str, Term] = _terms(
    ("advocate", "Advocate", "Advocate", "Responding to a new issue before any work is planned."),
    ("assessing", "Assessing", "Assessing", "Deciding whether the card is ready to be worked."),
    ("architecting", "Architecting", "Architecting", "Planning the change."),
    ("implementing", "Implementing", "Implementing", "Writing the change and opening the pull request."),
    ("reviewing", "Reviewing", "Reviewing", "Reviewing the pull request."),
    ("security", "Security", "Security", "Checking the change for security problems."),
    ("qa", "Qa", "Qa", "Testing the change against the card's acceptance criteria."),
    ("documenting", "Documenting", "Documenting", "Updating the documentation for the change."),
    ("closing_review", "Closing Review", "Closing Reviews", "Confirming earlier review feedback was addressed."),
    ("env_bootstrap", "Env Bootstrap", "Env Bootstrap", "Building the cached development environment performers start from."),
)

#: Who does the work (the keys of ``lifecycle.ROLE_TO_STAGE`` and the other
#: roles a performer can be configured for).
AGENTS: dict[str, Term] = _terms(
    ("advocate", "Advocate", "Advocates", "Responds to new issues."),
    ("assessor", "Assessor", "Assessors", "Decides whether a card is ready to be worked."),
    ("architect", "Architect", "Architects", "Plans the change."),
    ("implementer", "Implementer", "Implementers", "Writes the change and opens the pull request."),
    ("reviewer", "Reviewer", "Reviewers", "Reviews the pull request."),
    ("security", "Security", "Security", "Checks the change for security problems."),
    ("qa", "Qa", "Qa", "Tests the change against the card's acceptance criteria."),
    ("tech_writer", "Tech Writer", "Tech Writers", "Updates the documentation for the change."),
    ("closer", "Closer", "Closers", "Confirms earlier review feedback was addressed."),
    ("curator", "Curator", "Curators", "Triages incoming issues."),
    ("env_bootstrap", "Env Bootstrap", "Env Bootstrap", "Builds the cached development environment performers start from."),
    ("diagnostic", "Diagnostic", "Diagnostics", "Runs a free-form probe outside the card pipeline."),
)

#: How one conductor check ended.
OUTCOMES: dict[str, Term] = _terms(
    ("success", "success", "success", "The check completed."),
    ("error", "error", "errors", "The check failed."),
    ("circuit_open", "circuit_open", "circuit_open", "The check was skipped because a dependency is failing repeatedly."),
)

#: The nouns the dashboard is built from. These have no internal code of
#: their own; they are what the Glossary page explains.
CONCEPTS: dict[str, Term] = _terms(
    ("symphony", "Symphony", "Symphonies", "One GitHub Project board and the repository coordinare works it against."),
    ("stage", "Stage", "Stages", "A step in a card's pipeline, such as implementing or reviewing."),
    ("agent", "Agent", "Agents", "The persona that does the work of a stage, such as the implementer."),
    ("run", "Run", "Runs", "One agent working on one stage of one card, from dispatch to result."),
    ("check", "Check", "Checks", "One pass of the conductor over the board to see what needs doing."),
)

GROUPS: dict[str, dict[str, Term]] = {
    "phases": PHASES,
    "stages": STAGES,
    "agents": AGENTS,
    "outcomes": OUTCOMES,
    "concepts": CONCEPTS,
}

#: Lookup order for a bare code. A code that names both a stage and an agent
#: (``security``, ``qa``, ``advocate``) carries the same label in both, which
#: ``tests/unit/test_136_vocabulary.py`` enforces, so the order never changes
#: what is shown.
_LOOKUP_ORDER = ("phases", "stages", "agents", "outcomes")


def label(code: str | None) -> str:
    """The display label for an internal code, or the code itself when unknown.

    An unknown code is shown as it is, not prettified: a label that was never
    written should look like one.
    """
    key = str(code or "")
    for group in _LOOKUP_ORDER:
        term = GROUPS[group].get(key)
        if term is not None:
            return term.label
    return key


def labels() -> dict[str, str]:
    """Flat ``code -> label`` map across phases, stages, agents and outcomes."""
    flat: dict[str, str] = {}
    for group in reversed(_LOOKUP_ORDER):
        flat.update({code: term.label for code, term in GROUPS[group].items()})
    return flat


def payload() -> dict[str, Any]:
    """The whole glossary as plain data, for ``/api/vocabulary`` and the page."""
    return {
        "labels": labels(),
        **{name: [asdict(term) for term in terms.values()] for name, terms in GROUPS.items()},
    }


def javascript() -> str:
    """The glossary as the script ``/static/vocabulary.js`` serves."""
    data = json.dumps(payload(), sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return (
        "// 136: generated from src/coordinare/vocabulary.py. Do not edit; change the module.\n"
        f"var COORDINARE_VOCABULARY = {data};\n"
    )
