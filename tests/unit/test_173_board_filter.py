"""Spec 173 FR-022: card selection excludes only escalated issues."""
from __future__ import annotations


def _eligible(item_labels: dict, todo: list[str], *, handled: str, escalation: str) -> list[str]:
    """The filter as check_board applies it, extracted for a direct test."""
    advocate_labels = {escalation} if escalation else set()
    return [i for i in todo if not (set(item_labels.get(i, [])) & advocate_labels)]


def test_an_answered_issue_can_still_become_work() -> None:
    """The trap this fixes: a feature request the advocate acknowledged was
    excluded forever, invisible to the board with nothing saying so."""
    eligible = _eligible({"c1": ["advocate-handled"]}, ["c1"],
                         handled="advocate-handled", escalation="needs-human")
    assert eligible == ["c1"]


def test_an_escalated_issue_stays_excluded() -> None:
    """A human owns it; picking it up works against them."""
    eligible = _eligible({"c1": ["needs-human"]}, ["c1"],
                         handled="advocate-handled", escalation="needs-human")
    assert eligible == []


def test_the_real_node_narrows_the_same_way() -> None:
    from pathlib import Path

    source = Path("src/coordinare/graph/nodes/check_board.py").read_text()
    block = source[source.index("advocate_labels = set()"):source.index("item_labels = board.get")]
    assert "advocate_handled_label" not in block, (
        "the handled label must no longer take an issue out of the eligible set"
    )
    assert "advocate_escalation_label" in block


def test_the_lifecycle_table_is_numbered_without_a_gap() -> None:
    """FR-025. Renumbering by hand is exactly the part that goes wrong."""
    import re
    from pathlib import Path

    text = Path("docs/onboarding/03-performer-lifecycle.md").read_text()
    numbers = [int(m) for m in re.findall(r"^\| (\d+) \| `", text, re.MULTILINE)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert len(numbers) == 8


def test_no_document_still_calls_the_advocate_a_board_curator() -> None:
    from pathlib import Path

    for rel in ("README.md", "docs/quickstart.md", "docs/onboarding/03-performer-lifecycle.md"):
        text = Path(rel).read_text()
        for line in text.splitlines():
            if "`advocate`" in line or "| Advocate |" in line:
                assert "board" not in line.lower() or "curator" in line.lower(), (
                    f"{rel}: advocate row still mentions the board: {line!r}"
                )


def test_the_advocate_persona_no_longer_names_the_board_as_its_job() -> None:
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

    persona = DEFAULT_INSTRUCTIONS["advocate"]
    assert "Answer inbound GitHub issues" in persona
    assert "Adding anything to the project board" in persona, (
        "the persona must forbid the job the system prevents, not request it"
    )
    assert "curator" in DEFAULT_INSTRUCTIONS, "the board job moved to the role that owns it"
