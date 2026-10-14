"""One glossary for every internal code the dashboard shows (#136).

Before this module the dashboard had no label dictionary: ``humanPhase`` in
``helpers.js`` replaced underscores with spaces and title-cased whatever code
it was handed. Labels were therefore an accident of the code's spelling, and
the same concept could carry two or three names.

This is the single source of display labels. The dashboard reads it (it is
served as ``/static/vocabulary.js`` and as ``/api/vocabulary``), and a guard
test fails when a conductor phase, card stage, agent role or outcome exists
in the code without an entry here.

Conductor phases read as plain status words (#138). Stages and agents were
renamed by #137: a stage says what is
happening ("Implement"), an agent says who does it ("Implementer"), and the
same code can be both (``security`` is the Security stage and the Security
reviewer agent), so a label is always looked up by kind.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from coordinare.lifecycle import ROLE_TO_STAGE


@dataclass(frozen=True)
class Term:
    """One glossary entry: an internal code and how people read it."""

    code: str
    label: str
    plural: str
    description: str


def _terms(*rows: tuple[str, str, str, str]) -> dict[str, Term]:
    return {code: Term(code, label, plural, description) for code, label, plural, description in rows}


#: What the conductor itself is doing (``graph.state.DaemonPhase``), in plain
#: words (#138). These are the conductor's loop states, not a card's stage;
#: the dashboard shows them as a status and keeps the code in a tooltip.
PHASES: dict[str, Term] = _terms(
    ("idle", "Idle", "Idle", "The conductor has nothing to do for this card until the next check."),
    ("dispatching", "Starting an agent", "Starting an agent", "The conductor is starting an agent for the card's next stage."),
    ("monitoring_agent", "Watching an agent", "Watching an agent", "The conductor is watching a running agent."),
    ("monitoring_performer", "Watching an agent", "Watching an agent", "The conductor is watching a running agent."),
    ("monitoring_pr", "Waiting on review", "Waiting on review", "The card's pull request is waiting on checks or a human review."),
    ("merging", "Merging", "Merging", "The conductor is merging the card's pull request."),
    ("relay_feedback", "Sending feedback", "Sending feedback", "The conductor is passing review feedback back to an agent."),
    ("blocked", "Needs human", "Needs human", "The card is stopped until a person acts, and says why when the conductor knows."),
    ("recovery", "Recovering", "Recovering", "The conductor is recovering this card after a failed check."),
    ("system_error", "Error, will retry", "Errors, will retry", "The conductor hit an error on this card and will retry within its budget."),
    ("active", "Active", "Active", "A run whose exact state is not known yet."),
)

#: Where a card is in its pipeline: what is happening (#137). Keys are the
#: stage codes (the values of ``lifecycle.ROLE_TO_STAGE``).
STAGES: dict[str, Term] = _terms(
    ("advocate", "Respond", "Respond", "Responding to a new issue before any work is planned."),
    ("assessing", "Assess", "Assess", "Deciding whether the card is ready to be worked."),
    ("architecting", "Plan", "Plan", "Planning the change."),
    ("implementing", "Implement", "Implement", "Writing the change and opening the pull request."),
    ("reviewing", "Review", "Review", "Reviewing the pull request."),
    ("security", "Security", "Security", "Checking the change for security problems."),
    ("qa", "QA", "QA", "Testing the change against the card's acceptance criteria."),
    ("documenting", "Docs", "Docs", "Updating the documentation for the change."),
    ("closing_review", "Close", "Close", "Confirming earlier review feedback was addressed."),
    ("env_bootstrap", "Environment setup", "Environment setup", "Building the cached development environment performers start from."),
)

#: Who does the work: the agent (#137). Keys are the role codes (the keys of
#: ``lifecycle.ROLE_TO_STAGE`` and the other roles a performer can be
#: configured for). Each agent has exactly one display name.
AGENTS: dict[str, Term] = _terms(
    ("advocate", "Issue responder", "Issue responders", "Responds to new issues."),
    ("assessor", "Assessor", "Assessors", "Decides whether a card is ready to be worked."),
    ("architect", "Architect", "Architects", "Plans the change."),
    ("implementer", "Implementer", "Implementers", "Writes the change and opens the pull request."),
    ("reviewer", "Reviewer", "Reviewers", "Reviews the pull request."),
    ("security", "Security reviewer", "Security reviewers", "Checks the change for security problems."),
    ("qa", "QA", "QA", "Tests the change against the card's acceptance criteria."),
    ("tech_writer", "Documenter", "Documenters", "Updates the documentation for the change."),
    ("closer", "Closer", "Closers", "Confirms earlier review feedback was addressed."),
    ("curator", "Intake (issue triage)", "Intake (issue triage)", "Triages incoming issues. Works outside the card pipeline."),
    ("env_bootstrap", "Environment builder", "Environment builders", "Builds the cached development environment performers start from."),
    ("diagnostic", "Diagnostic", "Diagnostics", "Runs a free-form probe outside the card pipeline."),
)

#: Which agent does each stage (#137). The dashboard shows them as a pair,
#: "Implement · Implementer". Derived from the lifecycle table so the two can
#: never drift; ``env_bootstrap`` is not a lifecycle role and maps to itself.
STAGE_AGENT: dict[str, str] = {**{stage: role for role, stage in ROLE_TO_STAGE.items()}, "env_bootstrap": "env_bootstrap"}

#: How one conductor check ended.
OUTCOMES: dict[str, Term] = _terms(
    ("success", "success", "success", "The check completed."),
    ("error", "error", "errors", "The check failed."),
    ("circuit_open", "circuit_open", "circuit_open", "The check was skipped because a dependency is failing repeatedly."),
)

#: The nouns the dashboard is built from. These have no internal code of
#: their own; they are what the Glossary page explains.
CONCEPTS: dict[str, Term] = _terms(
    ("symphony", "Symphony", "Symphonies", "One GitHub Project and the repository coordinare works it against. The dashboard's symphony switcher filters every panel to one symphony or to all of them."),
    ("github_project", "GitHub Project", "GitHub Projects", "The GitHub Project a symphony takes its cards from. The dashboard shows its number as #N."),
    ("repository", "Repository", "Repositories", "The GitHub repository a symphony's pull requests are opened in. The dashboard shows it as owner/repo."),
    ("cards", "Cards", "Cards", "The cards of a symphony's GitHub Project, in the columns To Do, Blocked, In Progress and In Review."),
    ("stage", "Stage", "Stages", "A step in a card's pipeline, such as implementing or reviewing."),
    ("agent", "Agent", "Agents", "The persona that does the work of a stage, such as the implementer."),
    ("run", "Run", "Runs", "One agent working on one stage of one card, from dispatch to result."),
    ("run_id", "Run id", "Run ids", "How a run is named on screen: run and the first six characters of its id, such as run 299a83. Hover it for the full id, or use its Copy button."),
    ("run_detail", "Run detail", "Run details", "The view of one run: its live events, stderr, process metrics and steps. Reached from the Performers page, the Active performers tile or a card's View run link."),
    ("container", "Container", "Containers", "The Docker container a run's agent runs in. The run detail's technical section shows it; no other view does."),
    ("check", "Check", "Checks", "One pass of the conductor over the GitHub Project to see what needs doing."),
    ("revision_round", "Revision round", "Revision rounds", "One loop of review feedback going back to the implementer on a pull request."),
    ("progress_check", "Progress check", "Progress checks", "An automatic judgement of whether a running agent is still making progress."),
    ("needs_human", "Needs human", "Needs human", "A card stopped until a person acts. Its question or reason is listed in the needs-attention banner on the Dashboard."),
    ("settings", "Settings", "Settings", "Where coordinare's configuration is edited, in one tab per kind of setting. Each setting has exactly one editor."),
    ("model_server", "Model server", "Model servers", "Where model calls go: a self-hosted server at a base URL, or a vendor API. Models name one of these."),
    ("model", "Model", "Models", "A model name served by one model server. Modes choose their models from this list."),
    ("mode", "Mode", "Modes", "How a performer role uses models: one model, or a planner and an executor. Each role picks one mode."),
    ("routing", "Routing", "Routing", "Which base URL and wire format a self-hosted backend is sent to, for a backend and model. Applies to the next performer job."),
)

#: Why a card in the Blocked column needs a person, when the conductor knows (#142).
#: Keys are the cause codes ``services/block_cause.py`` names. The chip reads
#: "Needs human: <label>"; a card with no known cause shows the bare kind.
BLOCK_CAUSES: dict[str, Term] = _terms(
    ("closed_pr", "the pull request was closed without merging", "the pull request was closed without merging", "The card's pull request was closed by someone other than coordinare, so the card is parked."),
    ("env_blocked", "the environment needs repair", "the environment needs repair", "The card's checks could not run because of the environment, not the code, so the environment has to be fixed first."),
    ("output_truncated", "the model ran out of output room", "the model ran out of output room", "The model's answer was cut off before it finished."),
    ("format_exhausted", "retries exhausted on a format failure", "retries exhausted on a format failure", "The performer's output could not be read after every retry."),
    ("system_error", "the performer reported an error", "the performer reported an error", "The performer failed and the conductor has stopped retrying."),
)

#: Why a card is not progressing, shown on To Do and Blocked cards (#149).
#: Keys are the wait kinds the dashboard names, not the conductor's wire reasons.
WAITS: dict[str, Term] = _terms(
    ("queued", "Queued", "Queued", "Every card slot is in use, so the card waits for a free one."),
    ("waiting_on", "Waiting on", "Waiting on", "The card depends on another card that is not done yet."),
    ("needs_human", "Needs human", "Needs human", "The conductor needs a person to answer or decide before the card goes on."),
    ("filtered", "Filtered out", "Filtered out", "The card is not coordinare's to work, because of who it is assigned to."),
)

GROUPS: dict[str, dict[str, Term]] = {
    "phases": PHASES,
    "stages": STAGES,
    "agents": AGENTS,
    "outcomes": OUTCOMES,
    "concepts": CONCEPTS,
    "waits": WAITS,
    "block_causes": BLOCK_CAUSES,
}

def _label(group: dict[str, Term], code: str | None) -> str:
    key = str(code or "")
    term = group.get(key)
    return term.label if term is not None else key


def phase_label(code: str | None) -> str:
    """Display label for a conductor phase, or the code itself when unknown."""
    return _label(PHASES, code)


def stage_label(code: str | None) -> str:
    """Display label for a card stage ("Implement"), or the code when unknown."""
    return _label(STAGES, code)


def agent_label(code: str | None) -> str:
    """Display label for an agent role ("Implementer"), or the code when unknown."""
    return _label(AGENTS, code)


def stage_with_agent(stage: str | None) -> str:
    """A stage shown with the agent that does it: "Implement · Implementer".

    An unknown stage, or one with no agent, is shown alone. An unknown code is
    shown as it is, not prettified: a label that was never written should look
    like one.
    """
    key = str(stage or "")
    agent = STAGE_AGENT.get(key)
    if key not in STAGES or agent is None:
        return stage_label(key)
    return f"{stage_label(key)} · {agent_label(agent)}"


def block_cause_label(code: str | None) -> str:
    """Display label for why a stopped card needs a person, or the code when unknown."""
    return _label(BLOCK_CAUSES, code)


def label(code: str | None) -> str:
    """Display label for a conductor phase or a check outcome.

    Stages and agents are deliberately not reachable here: several codes are
    both, with different labels, so they must be looked up by kind.
    """
    key = str(code or "")
    for group in (PHASES, OUTCOMES):
        term = group.get(key)
        if term is not None:
            return term.label
    return key


def labels() -> dict[str, str]:
    """Flat ``code -> label`` map for conductor phases and check outcomes."""
    return {**{c: t.label for c, t in OUTCOMES.items()}, **{c: t.label for c, t in PHASES.items()}}


def payload() -> dict[str, Any]:
    """The whole glossary as plain data, for ``/api/vocabulary`` and the page."""
    return {
        "labels": labels(),
        "stage_labels": {code: term.label for code, term in STAGES.items()},
        "agent_labels": {code: term.label for code, term in AGENTS.items()},
        "wait_labels": {code: term.label for code, term in WAITS.items()},
        "block_cause_labels": {code: term.label for code, term in BLOCK_CAUSES.items()},
        "stage_agent": dict(STAGE_AGENT),
        **{name: [asdict(term) for term in terms.values()] for name, terms in GROUPS.items()},
    }


def javascript() -> str:
    """The glossary as the script ``/static/vocabulary.js`` serves."""
    data = json.dumps(payload(), sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return (
        "// 136: generated from src/coordinare/vocabulary.py. Do not edit; change the module.\n"
        f"var COORDINARE_VOCABULARY = {data};\n"
    )
