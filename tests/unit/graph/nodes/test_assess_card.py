from __future__ import annotations

import pytest

from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.state import initial_state


class _GitHub:
    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}


class _Backend:
    async def assess(self, card):
        _ = card
        return {"sufficient": False, "questions": ["Need context"]}


class _FailingBackend:
    async def assess(self, card):
        msg = "Backend unavailable"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_assess_card_blocks_on_service_failure() -> None:
    """Spec edge case: assessment failures move card to blocked, not idle."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _FailingBackend()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert any("Assessment failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_assess_card_sets_blocked_when_insufficient() -> None:
    state = initial_state()
    state["current_card"] = {"issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _Backend()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need context"]


# ---------------------------------------------------------------------------
# Lines 27-29: clarifications appended to details when present
# ---------------------------------------------------------------------------


class _SufficientBackend:
    async def assess(self, card):
        return {"sufficient": True, "questions": []}


@pytest.mark.asyncio
async def test_assess_card_dispatches_with_no_clarifications() -> None:
    """Lines 62->66: sufficient with no clarifications → skip embedding, set dispatching."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    # No card_clarifications set

    result = await assess_card(state)

    assert result["phase"] == "dispatching"
    assert result["open_questions"] == []


@pytest.mark.asyncio
async def test_assess_card_merges_clarifications_into_details() -> None:
    """Lines 27-29: when card_clarifications exist, they're merged into the issue details."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    state["card_clarifications"] = [{"question": "Q?", "answer": "A"}]

    result = await assess_card(state)

    # Sufficient → dispatching
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# Lines 51-57: no new questions after answered rounds → treat as sufficient
# ---------------------------------------------------------------------------


class _InsufficientNoQuestionsBackend:
    """Returns insufficient=False but no questions."""
    async def assess(self, card):
        return {"sufficient": False, "questions": []}


@pytest.mark.asyncio
async def test_assess_card_treats_no_new_questions_after_answers_as_sufficient() -> None:
    """Lines 51-57: insufficient=False + no questions + answered rounds → sufficient."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _InsufficientNoQuestionsBackend()
    # Provide answered clarifications (at least one round answered)
    state["card_clarifications"] = [{"question": "Q?", "answer": "Yes"}]

    result = await assess_card(state)

    # Should be treated as sufficient because there are no new questions
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# Lines 62-67: clarifications embedded into card when sufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_card_embeds_clarifications_into_card_when_sufficient() -> None:
    """Lines 62-67: clarifications are embedded into current_card when assessment is sufficient."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1", "title": "Fix bug"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    state["card_clarifications"] = [{"question": "Q?", "answer": "A"}]

    result = await assess_card(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"].get("clarifications") == [{"question": "Q?", "answer": "A"}]


# ---------------------------------------------------------------------------
# 018 — Assessor persona injection tests (T011)
# ---------------------------------------------------------------------------


class _DetailsCapture:
    """Backend that captures the details dict passed to assess()."""

    def __init__(self) -> None:
        self.last_details: dict | None = None

    async def assess(self, card):
        self.last_details = card
        return {"sufficient": True, "questions": []}


def _make_config_with_persona(instructions: str):
    from coordinare.config import PersonaConfig, PersonasConfig

    class _FakeConfig:
        pass

    cfg = _FakeConfig()
    cfg.personas = PersonasConfig(assessor=PersonaConfig(instructions=instructions))
    return cfg


@pytest.mark.asyncio
async def test_assessor_persona_instructions_present_in_details_when_configured() -> None:
    """FR-005: custom assessor persona reaches the backend details dict."""
    backend = _DetailsCapture()
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = backend
    state["config"] = _make_config_with_persona("Focus on business value.")

    await assess_card(state)

    assert backend.last_details is not None
    assert backend.last_details.get("persona_instructions") == "Focus on business value."


@pytest.mark.asyncio
async def test_assessor_default_instructions_used_when_no_custom_persona() -> None:
    """FR-007: empty persona config → default instructions injected."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS
    backend = _DetailsCapture()
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = backend
    state["config"] = _make_config_with_persona("")

    await assess_card(state)

    assert backend.last_details is not None
    instructions = backend.last_details.get("persona_instructions")
    assert instructions == DEFAULT_INSTRUCTIONS["assessor"]
    assert instructions  # non-empty


def test_assess_prompt_starts_with_assessor_instructions_when_set() -> None:
    """T010: persona_instructions are prepended to the assessment prompt."""
    from coordinare.services.assessment import _build_assess_prompt
    card = {
        "title": "My card",
        "body": "Some description",
        "persona_instructions": "Focus on business value.",
    }
    prompt = _build_assess_prompt(card)
    assert prompt.startswith("## Assessor Instructions\nFocus on business value.")


def test_assess_prompt_unchanged_when_persona_instructions_absent() -> None:
    """T010: prompt is unmodified when persona_instructions key is absent."""
    from coordinare.services.assessment import _build_assess_prompt
    card = {"title": "My card", "body": "Some description"}
    prompt = _build_assess_prompt(card)
    assert not prompt.startswith("## Assessor Instructions")
    assert "You are reviewing" in prompt


# --- 046: Assessor dependency detection ---


class _DepBackend:
    """Backend that returns a dependency verdict."""
    def __init__(self, deps: list[int]):
        self._deps = deps

    async def assess(self, card):
        return {"sufficient": False, "dependencies": self._deps, "questions": []}


@pytest.mark.asyncio
async def test_assessor_receives_active_card_titles() -> None:
    """046 T023: The assessor backend receives an active_cards list
    containing titles of other board cards for implicit dep detection."""
    received = {}

    class _CapturingBackend:
        async def assess(self, card):
            received.update(card)
            return {"sufficient": True}

    state = initial_state()
    state["current_card"] = {"id": "CARD_B", "issue_id": "ISSUE_B"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _CapturingBackend()
    state["board_snapshot"] = {"TODO": [], "IN_PROGRESS": ["CARD_A"], "IN_REVIEW": []}
    state["_board_titles"] = {"CARD_A": "Implement theming"}
    state["_board_issue_numbers"] = {"CARD_A": 42}

    await assess_card(state)

    assert "active_cards" in received
    titles = [c["title"] for c in received["active_cards"]]
    assert "Implement theming" in titles


@pytest.mark.asyncio
async def test_assessor_dependency_blocks_card() -> None:
    """046 T024: When the assessor returns dependencies=[42], the card
    is blocked with a question explaining the dependency."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_B", "issue_id": "ISSUE_B"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _DepBackend([42])
    state["board_snapshot"] = {}

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    questions = result.get("open_questions", [])
    assert any("#42" in q for q in questions)


@pytest.mark.asyncio
async def test_assessor_no_dependency_proceeds() -> None:
    """046: Assessor returns no dependencies → card proceeds normally."""
    class _NoDepsBackend:
        async def assess(self, card):
            return {"sufficient": True, "dependencies": []}

    state = initial_state()
    state["current_card"] = {"id": "CARD_B", "issue_id": "ISSUE_B"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _NoDepsBackend()
    state["board_snapshot"] = {}

    result = await assess_card(state)

    assert result["phase"] == "dispatching"
