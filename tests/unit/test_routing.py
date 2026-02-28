"""Unit tests for advocate label filtering in board check (007, T010)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_advocate_labeled_todo_items_not_dispatched() -> None:
    """Issues with advocate-handled label in TODO are skipped — not dispatched."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"TODO": ["item-1"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
        "titles": {"item-1": "A question about billing"},
        "descriptions": {"item-1": "I have a billing question."},
        "issue_numbers": {"item-1": 1},
        "item_labels": {"item-1": ["advocate-handled"]},
    }

    state = {
        "github_service": github,
        "advocate_handled_label": "advocate-handled",
        "advocate_escalation_label": "needs-human",
        "advocate_history": set(),
    }
    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_escalation_labeled_todo_items_not_dispatched() -> None:
    """Issues with needs-human label in TODO are skipped."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"TODO": ["item-1"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
        "titles": {"item-1": "Sensitive issue"},
        "descriptions": {"item-1": "billing billing billing"},
        "issue_numbers": {"item-1": 1},
        "item_labels": {"item-1": ["needs-human"]},
    }

    state = {
        "github_service": github,
        "advocate_handled_label": "advocate-handled",
        "advocate_escalation_label": "needs-human",
        "advocate_history": set(),
    }
    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_unlabeled_todo_items_dispatched() -> None:
    """Unlabeled TODO items proceed to dispatch as normal."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"TODO": ["item-1"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
        "titles": {"item-1": "Implement feature X"},
        "descriptions": {"item-1": "Feature description."},
        "issue_numbers": {"item-1": 2},
        "item_labels": {},  # no labels
    }

    state = {
        "github_service": github,
        "advocate_handled_label": "advocate-handled",
        "advocate_escalation_label": "needs-human",
        "advocate_history": set(),
    }
    result = await check_board(state)

    assert result["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_no_advocate_labels_configured_does_not_filter() -> None:
    """When advocate is disabled (no label config), all TODO items are eligible."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"TODO": ["item-1"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
        "titles": {"item-1": "Task A"},
        "descriptions": {"item-1": "Description."},
        "issue_numbers": {"item-1": 1},
        "item_labels": {"item-1": ["some-other-label"]},
    }

    state = {
        "github_service": github,
        "advocate_handled_label": "",  # not configured
        "advocate_escalation_label": "",
        "advocate_history": set(),
    }
    result = await check_board(state)

    assert result["phase"] == "dispatching"
