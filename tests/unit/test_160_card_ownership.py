"""160: coordinare works its own cards, and only its own.

``assignee_filter`` (spec 050) decided TODO pickup and nothing else, so a card a
human dragged into *In progress*, one sitting in *Blocked*, or a PR in *In
review* was adopted whoever it belonged to. These tests pin the gate at every
path that adopts a card without an existing session, and pin the two places it
must NOT reach: a card already in flight, and the disappeared-card check.
"""

from __future__ import annotations

import pytest
import structlog.testing

from coordinare.dashboard import _DASHBOARD_HTML, ownership_hint
from coordinare.graph.nodes import check_board as cb_mod
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state
from coordinare.services.card_ownership import (
    OwnershipPolicy,
    filter_owned,
    ownership_policy,
    owns_card,
)

BOT = "coordinare-bot"
HUMAN = "human-engineer"


class _Config:
    """Only the two attributes the policy reads, so a missing one is a real
    ``getattr`` default rather than a MagicMock that answers everything."""

    def __init__(self, assignee_filter=None, include_unassigned=False):
        self.assignee_filter = assignee_filter
        self.include_unassigned = include_unassigned


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


class TestOwnershipPolicy:
    def test_no_config_is_inactive(self) -> None:
        assert ownership_policy(None).active is False

    def test_no_filter_is_inactive(self) -> None:
        assert ownership_policy(_Config()).active is False

    def test_include_unassigned_alone_is_a_complete_policy(self) -> None:
        """The GitHub App case, and the reason the union model exists.

        An App cannot be assigned to an issue, so a deployment authenticating as
        one has no login to name. `include_unassigned` alone then reads: work
        what nobody has claimed, leave what a human has claimed. Eligibility is
        `{assigned to login} | {unassigned}` and the first half is empty.

        This reverses an earlier decision, in which the flag was a modifier of
        `assignee_filter` and did nothing without it -- expressible only by
        inventing a dummy login nobody would ever be assigned.
        """
        policy = ownership_policy(_Config(include_unassigned=True))
        assert policy.active is True
        assert policy.include_unassigned is True
        assert owns_card(policy, "C1", {"C1": []}) is True       # nobody claimed it
        assert owns_card(policy, "C2", {"C2": [HUMAN]}) is False  # a human claimed it
        assert owns_card(policy, "C3", {}) is True                # no entry == unclaimed

    def test_neither_opt_in_leaves_the_board_untouched(self) -> None:
        """The default every deployment has today."""
        policy = ownership_policy(_Config())
        assert policy.active is False
        assert owns_card(policy, "C1", {"C1": [HUMAN]}) is True

    def test_empty_login_never_matches_an_empty_assignee_string(self) -> None:
        """With `include_unassigned` alone the login is "", and an unguarded
        `"" in assignees` would admit a card through the half of the union that
        is supposed to be empty."""
        policy = ownership_policy(_Config(include_unassigned=True))
        assert owns_card(policy, "C1", {"C1": [""]}) is False
        assert owns_card(policy, "C2", {"C2": ["", HUMAN]}) is False

    def test_include_unassigned_is_read_from_config(self) -> None:
        """The value reaches the policy, both ways round."""
        assert ownership_policy(_Config(assignee_filter=BOT, include_unassigned=True)).include_unassigned is True
        assert ownership_policy(_Config(assignee_filter=BOT, include_unassigned=False)).include_unassigned is False

    def test_include_unassigned_is_coerced_to_bool(self) -> None:
        # pydantic gives a real bool, but ownership_policy reads by getattr off a
        # loosely typed object; a truthy string must not arrive as a string.
        policy = ownership_policy(_Config(assignee_filter=BOT, include_unassigned="yes"))
        assert policy.include_unassigned is True

    def test_filter_is_normalised(self) -> None:
        # poll_board lowercases the logins it returns, so the filter has to be
        # lowered here or the comparison is case-sensitive by accident.
        policy = ownership_policy(_Config(assignee_filter="  Coordinare-Bot  "))
        assert policy.login == BOT
        assert owns_card(policy, "C1", {"C1": [BOT]}) is True

    def test_non_string_filter_is_ignored(self) -> None:
        assert ownership_policy(_Config(assignee_filter=123)).active is False

    def test_config_without_the_attribute_defaults_off(self) -> None:
        # Older configs (and every test double predating this spec) have no
        # include_unassigned attribute at all.
        class _Old:
            assignee_filter = BOT

        assert ownership_policy(_Old()).include_unassigned is False


class TestOwnsCard:
    def test_inactive_policy_owns_everything(self) -> None:
        assert owns_card(OwnershipPolicy(), "C1", {"C1": [HUMAN]}) is True

    def test_assigned_to_filter_login(self) -> None:
        assert owns_card(OwnershipPolicy(BOT), "C1", {"C1": [BOT]}) is True

    def test_assigned_alongside_others(self) -> None:
        assert owns_card(OwnershipPolicy(BOT), "C1", {"C1": [HUMAN, BOT]}) is True

    def test_assigned_to_someone_else(self) -> None:
        assert owns_card(OwnershipPolicy(BOT), "C1", {"C1": [HUMAN]}) is False

    def test_unassigned_excluded_by_default(self) -> None:
        assert owns_card(OwnershipPolicy(BOT), "C1", {"C1": []}) is False

    def test_unassigned_included_when_opted_in(self) -> None:
        policy = OwnershipPolicy(BOT, include_unassigned=True)
        assert owns_card(policy, "C1", {"C1": []}) is True

    def test_opting_in_to_unassigned_does_not_admit_other_people(self) -> None:
        # The whole point of the filter: "also take what nobody claimed" is not
        # "also take what someone else claimed".
        policy = OwnershipPolicy(BOT, include_unassigned=True)
        assert owns_card(policy, "C1", {"C1": [HUMAN]}) is False

    def test_missing_entry_is_treated_as_unassigned(self) -> None:
        # An absent key and an empty list are the same fact. Guessing the other
        # way would let a card coordinare knows nothing about through the filter.
        assert owns_card(OwnershipPolicy(BOT), "C1", {}) is False
        assert owns_card(OwnershipPolicy(BOT, include_unassigned=True), "C1", {}) is True

    def test_none_value_is_treated_as_unassigned(self) -> None:
        assert owns_card(OwnershipPolicy(BOT), "C1", {"C1": None}) is False


class TestFilterOwned:
    def test_inactive_policy_passes_everything_through(self) -> None:
        ids = ["C1", "C2"]
        assert filter_owned(OwnershipPolicy(), ids, {"C1": [HUMAN]}) == ids

    def test_preserves_order(self) -> None:
        assignees = {"C1": [BOT], "C2": [HUMAN], "C3": [BOT]}
        assert filter_owned(OwnershipPolicy(BOT), ["C3", "C2", "C1"], assignees) == ["C3", "C1"]

    def test_returns_a_new_list(self) -> None:
        ids = ["C1"]
        assert filter_owned(OwnershipPolicy(), ids, {}) is not ids


# ---------------------------------------------------------------------------
# Every adoption path in check_board
# ---------------------------------------------------------------------------


class _Board:
    """A board fake placing cards in arbitrary columns with arbitrary assignees."""

    def __init__(self, snapshot: dict, assignees: dict) -> None:
        self._snapshot = snapshot
        self._assignees = assignees
        self.detail_reads: list[str] = []
        self.moves: list[tuple[str, str]] = []

    async def poll_board(self) -> dict:
        columns = {"TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []}
        columns.update(self._snapshot)
        ids = [cid for col in columns.values() for cid in col]
        return {
            "snapshot": columns,
            "titles": {cid: f"Card {cid}" for cid in ids},
            "descriptions": dict.fromkeys(ids, ""),
            "issue_numbers": {cid: i + 1 for i, cid in enumerate(ids)},
            "item_assignees": self._assignees,
        }

    async def get_issue_details(self, issue_id: str) -> dict:
        self.detail_reads.append(issue_id)
        return {"comments": {"nodes": []}}

    async def move_card(self, item_id: str, status: str) -> None:
        self.moves.append((item_id, status))


class _Performer:
    """Records anything check_board relays -- a cancellation above all."""

    def __init__(self) -> None:
        self.relayed: list[dict] = []

    async def relay_feedback(self, payload: dict) -> None:
        self.relayed.append(payload)


def _state(board: _Board, config: _Config) -> dict:
    state = initial_state()
    state["github_service"] = board
    state["config"] = config
    return state


@pytest.mark.asyncio
async def test_todo_pickup_skips_foreign_card() -> None:
    board = _Board({"TODO": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_todo_pickup_takes_own_card() -> None:
    board = _Board({"TODO": ["MINE"]}, {"MINE": [BOT]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["current_card"]["id"] == "MINE"


@pytest.mark.asyncio
async def test_todo_pickup_takes_unassigned_when_opted_in() -> None:
    board = _Board({"TODO": ["NOBODYS"]}, {"NOBODYS": []})
    config = _Config(assignee_filter=BOT, include_unassigned=True)
    result = await check_board(_state(board, config))
    assert result["current_card"]["id"] == "NOBODYS"


@pytest.mark.asyncio
async def test_todo_pickup_still_skips_others_when_opted_in() -> None:
    board = _Board({"TODO": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    config = _Config(assignee_filter=BOT, include_unassigned=True)
    result = await check_board(_state(board, config))
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_app_auth_scenario_unassigned_worked_claimed_left_alone() -> None:
    """The deployment shape this feature is for: authenticating as a GitHub App,
    so no assignable login exists. Unassigned cards are coordinare's; anything a
    human has claimed is not."""
    board = _Board(
        {"TODO": ["CLAIMED", "UNCLAIMED"]},
        {"CLAIMED": [HUMAN], "UNCLAIMED": []},
    )
    result = await check_board(_state(board, _Config(include_unassigned=True)))
    assert result["current_card"]["id"] == "UNCLAIMED"


@pytest.mark.asyncio
async def test_app_auth_scenario_leaves_a_claimed_board_entirely_alone() -> None:
    """Every card claimed by a teammate: coordinare works none of them and says
    so, rather than going quiet."""
    board = _Board(
        {"TODO": ["A"], "IN_PROGRESS": ["B"], "IN_REVIEW": ["C"], "BLOCKED": ["D"]},
        {"A": [HUMAN], "B": ["teammate"], "C": [HUMAN], "D": ["teammate"]},
    )
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, _Config(include_unassigned=True)))
    assert result["phase"] == "idle"
    assert not (result.get("active_sessions") or {})
    assert board.detail_reads == []          # no reminder posted on anyone's blocked card
    assert board.moves == []                 # nothing moved
    assert [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]


@pytest.mark.asyncio
async def test_cards_coordinare_will_not_work_are_still_visible() -> None:
    """Not eligible is not hidden. Only adoption consults ownership, so the board
    snapshot still carries every card for the dashboard to count and for
    dependency resolution to read across."""
    board = _Board(
        {"TODO": ["MINE", "THEIRS"], "IN_PROGRESS": ["ALSO_THEIRS"]},
        {"MINE": [], "THEIRS": [HUMAN], "ALSO_THEIRS": [HUMAN]},
    )
    result = await check_board(_state(board, _Config(include_unassigned=True)))
    snapshot = result["board_snapshot"]
    assert snapshot["TODO"] == ["MINE", "THEIRS"]
    assert snapshot["IN_PROGRESS"] == ["ALSO_THEIRS"]
    assert result["current_card"]["id"] == "MINE"


@pytest.mark.asyncio
async def test_in_progress_readoption_skips_foreign_card() -> None:
    """The gap spec 050 left: a human drags their own card into In progress and
    coordinare dispatches a performer at it."""
    board = _Board({"IN_PROGRESS": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert "FOREIGN" not in (result.get("active_sessions") or {})


@pytest.mark.asyncio
async def test_in_progress_readoption_takes_own_card() -> None:
    board = _Board({"IN_PROGRESS": ["MINE"]}, {"MINE": [BOT]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert "MINE" in (result.get("active_sessions") or {})


@pytest.mark.asyncio
async def test_in_review_readoption_skips_foreign_card() -> None:
    board = _Board({"IN_REVIEW": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert "FOREIGN" not in (result.get("active_sessions") or {})


@pytest.mark.asyncio
async def test_in_review_readoption_takes_own_card() -> None:
    board = _Board({"IN_REVIEW": ["MINE"]}, {"MINE": [BOT]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert "MINE" in (result.get("active_sessions") or {})


@pytest.mark.asyncio
async def test_blocked_handling_skips_foreign_card() -> None:
    """A foreign blocked card must not be read, reminded about, or resumed."""
    board = _Board({"BLOCKED": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert board.detail_reads == []
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_blocked_handling_takes_own_card() -> None:
    board = _Board({"BLOCKED": ["MINE"]}, {"MINE": [BOT]})
    result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_blocked_recovery_only_sees_owned_cards(monkeypatch) -> None:
    """129's recovery MOVES cards on the board. It has no business doing that to
    someone else's card, and it runs before the blocked branch's own filter."""
    seen: list[list] = []

    async def _spy(state, github, blocked, board):
        seen.append(list(blocked))

    monkeypatch.setattr(cb_mod, "_attempt_blocked_card_recovery", _spy)
    board = _Board({"BLOCKED": ["MINE", "FOREIGN"]}, {"MINE": [BOT], "FOREIGN": [HUMAN]})
    await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert seen == [["MINE"]]


# ---------------------------------------------------------------------------
# Where the gate must NOT reach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_in_flight_survives_losing_its_assignee() -> None:
    """Unassigning a card mid-flight must not strand a branch with a performer
    half-way through it. The gate decides only whether a NEW session is created."""
    board = _Board({"IN_PROGRESS": ["MINE"]}, {"MINE": []})
    state = _state(board, _Config(assignee_filter=BOT))
    state["active_sessions"] = {
        "MINE": {
            "card_id": "MINE",
            "phase": "monitoring_performer",
            "current_card": {"id": "MINE", "title": "Card MINE", "status": "IN_PROGRESS"},
            "performer_stage": "implementing",
        }
    }
    result = await check_board(state)
    assert "MINE" in (result.get("active_sessions") or {})


@pytest.mark.asyncio
async def test_in_flight_card_does_not_read_as_disappeared() -> None:
    """The sharper version of the same trap on the IN_PROGRESS column: filtering
    the column list rather than the adoption would make an in-flight card that
    lost its assignee look as though it had left the board entirely."""
    board = _Board({"IN_PROGRESS": ["MINE"]}, {"MINE": []})
    state = _state(board, _Config(assignee_filter=BOT))
    state["active_sessions"] = {
        "MINE": {
            "card_id": "MINE",
            "phase": "monitoring_performer",
            "current_card": {"id": "MINE", "title": "Card MINE", "status": "IN_PROGRESS"},
            "performer_stage": "implementing",
        }
    }
    state["current_card"] = {"id": "MINE", "title": "Card MINE", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["agent_dispatch"] = {"session_id": "S1"}
    # The damage a false "disappeared" does is the cancellation, not the end
    # state: _rederive_current_card restores current_card from the surviving
    # session either way, so asserting on that would pass over a killed
    # performer. Assert on the kill itself.
    performer = _Performer()
    state["agent_service"] = performer
    await check_board(state)
    assert performer.relayed == []
    assert board.moves == []


@pytest.mark.asyncio
async def test_foreign_blocked_card_does_not_read_as_disappeared() -> None:
    """The disappeared-card check asks "is the active card still ANYWHERE on the
    board". Asking it of the filtered list reads an unassignment as a deletion
    and cancels live work."""
    board = _Board({"BLOCKED": ["MINE"]}, {"MINE": [HUMAN]})
    state = _state(board, _Config(assignee_filter=BOT))
    state["current_card"] = {"id": "MINE", "title": "Card MINE", "status": "BLOCKED"}
    state["phase"] = "monitoring_performer"
    result = await check_board(state)
    # Cancellation resets phase to idle and clears the card; neither may happen.
    assert result.get("current_card", {}).get("id") == "MINE"
    assert board.moves == []


@pytest.mark.asyncio
async def test_board_where_nobody_is_assigned_is_named_not_silent() -> None:
    """The board `include_unassigned` exists for. Every card carries an empty
    assignee list, a filter is set, so nothing is eligible and coordinare goes
    quiet on a full board.

    Keyed off the item_assignees dict being empty, this warning could never fire
    in production: the GitHub poller seeds `item_assignees[item_id] = []` for
    every item before it reads the content, so the dict is empty only when the
    board is."""
    board = _Board({"TODO": ["C1", "C2"], "BLOCKED": ["C3"]}, {"C1": [], "C2": [], "C3": []})
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["phase"] == "idle"
    warned = [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]
    assert warned, "a full board yielding nothing must not be silent"
    assert warned[0]["cards_on_board"] == 3
    assert warned[0]["assignee_data_present"] is True


@pytest.mark.asyncio
async def test_the_real_board_shape_is_reported() -> None:
    """The measured website board in miniature: most cards unassigned, a few
    assigned to other people, none to coordinare. The narrower condition ("no card
    carries an assignee") is false here, so it would have stayed silent."""
    board = _Board(
        {"TODO": ["U1", "U2", "U3"], "IN_PROGRESS": ["H1"]},
        {"U1": [], "U2": [], "U3": [], "H1": [HUMAN]},
    )
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["phase"] == "idle"
    warned = [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]
    assert warned, "a filter that excludes the whole board must not be silent"
    assert warned[0]["cards_on_board"] == 4
    assert warned[0]["cards_with_an_assignee"] == 1
    assert warned[0]["include_unassigned"] is False


@pytest.mark.asyncio
async def test_the_warning_fires_once_per_episode_not_every_poll() -> None:
    """check_board runs once per poll while no session is adopted -- which is
    precisely this situation -- so an unlatched warning is 120 identical lines an
    hour at the default interval."""
    board = _Board({"TODO": ["C1"]}, {"C1": []})
    state = _state(board, _Config(assignee_filter=BOT))
    with structlog.testing.capture_logs() as first:
        await check_board(state)
    with structlog.testing.capture_logs() as second:
        await check_board(state)
    assert [e for e in first if e["event"] == "card_ownership.no_cards_owned"]
    assert not [e for e in second if e["event"] == "card_ownership.no_cards_owned"]


@pytest.mark.asyncio
async def test_the_warning_re_arms_once_the_board_recovers() -> None:
    """A board that goes wrong, is fixed, and goes wrong again says so again."""
    unassigned = _Board({"TODO": ["C1"]}, {"C1": []})
    state = _state(unassigned, _Config(assignee_filter=BOT))
    await check_board(state)  # warns, latches

    state["github_service"] = _Board({"TODO": ["C1"]}, {"C1": [BOT]})
    await check_board(state)  # condition cleared, latch released

    state["github_service"] = _Board({"TODO": ["C2"]}, {"C2": []})
    with structlog.testing.capture_logs() as logs:
        await check_board(state)
    assert [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]


@pytest.mark.asyncio
async def test_no_warning_while_the_filter_is_finding_work() -> None:
    """The warning is for a board that yields nothing, not for one where other
    people's cards are being skipped -- which is the filter working, every cycle."""
    board = _Board({"TODO": ["MINE", "FOREIGN"]}, {"MINE": [BOT], "FOREIGN": [HUMAN]})
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["current_card"]["id"] == "MINE"
    assert not [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]


@pytest.mark.asyncio
async def test_warning_when_someone_else_owns_the_whole_board() -> None:
    """Assignees are present and none are coordinare's -- the case an operator
    actually hits. Measured on the real website board: 73 cards, 8 with any
    assignee, so a warning keyed on "nobody is assigned anywhere" stays silent
    while coordinare drops from 73 workable cards to zero.

    This case was deliberately left un-warned until the latch existed, because
    unlatched it would fire every cycle on a shared board where the filter is
    working correctly. Once per episode is a signal; every cycle is noise."""
    board = _Board({"TODO": ["FOREIGN"]}, {"FOREIGN": [HUMAN]})
    with structlog.testing.capture_logs() as logs:
        await check_board(_state(board, _Config(assignee_filter=BOT)))
    warned = [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]
    assert warned
    # The three shapes an operator has to tell apart, distinguishable in the event.
    assert warned[0]["cards_on_board"] == 1
    assert warned[0]["cards_with_an_assignee"] == 1
    assert warned[0]["assignee_data_present"] is True


@pytest.mark.asyncio
async def test_opting_in_to_unassigned_silences_the_warning() -> None:
    """include_unassigned is the answer to an unassigned board, so a board it
    makes workable must not still be reported as unworkable."""
    board = _Board({"TODO": ["NOBODYS"]}, {"NOBODYS": []})
    config = _Config(assignee_filter=BOT, include_unassigned=True)
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, config))
    assert result["current_card"]["id"] == "NOBODYS"
    assert not [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]


@pytest.mark.asyncio
async def test_filtered_event_is_silent_when_nothing_is_skipped() -> None:
    """A board of coordinare's own cards must not log a filtered event with
    skipped=0 on every cycle."""
    board = _Board({"TODO": ["MINE"]}, {"MINE": [BOT]})
    with structlog.testing.capture_logs() as logs:
        await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert not [e for e in logs if e["event"] == "card_ownership.filtered"]


@pytest.mark.asyncio
async def test_board_without_assignee_data_is_named_not_silent() -> None:
    """A provider that reports no assignees at all, with a filter configured,
    excludes every card. Coordinare then looks idle on a full board, which is the
    hardest failure of all to diagnose -- so it says so."""

    class _NoAssignees(_Board):
        async def poll_board(self) -> dict:
            board = await super().poll_board()
            board.pop("item_assignees")
            return board

    board = _NoAssignees({"TODO": ["C1"]}, {})
    with structlog.testing.capture_logs() as logs:
        result = await check_board(_state(board, _Config(assignee_filter=BOT)))
    assert result["phase"] == "idle"
    warned = [e for e in logs if e["event"] == "card_ownership.no_cards_owned"]
    assert warned and warned[0]["cards_on_board"] == 1


@pytest.mark.asyncio
async def test_no_filter_configured_adopts_everything() -> None:
    """The pre-050 behaviour every deployment without a filter still has."""
    board = _Board(
        {"TODO": ["T"], "IN_PROGRESS": ["P"], "IN_REVIEW": ["R"]},
        {"T": [HUMAN], "P": [HUMAN], "R": [HUMAN]},
    )
    result = await check_board(_state(board, _Config()))
    sessions = result.get("active_sessions") or {}
    assert {"P", "R"} <= set(sessions)
    assert result["phase"] != "idle"


# ---------------------------------------------------------------------------
# What the dashboard says the policy is (FR-010)
# ---------------------------------------------------------------------------


class TestOwnershipHint:
    """The hint is a claim about the gate, so it is derived from the gate.

    Before eligibility became a union, ``include_unassigned`` did nothing on its
    own and naming the login alone was the whole truth. It is now an independent
    opt-in and can be the entire policy, which is the only shape available to a
    deployment authenticating as a GitHub App.
    """

    def test_no_policy_is_the_empty_string(self) -> None:
        """An empty hint is itself a claim: every card on the board is eligible."""
        assert ownership_hint(_Config()) == ""

    def test_a_login_alone(self) -> None:
        assert ownership_hint(_Config(assignee_filter="coordinare-bot")) == "coordinare-bot"

    def test_both_halves_of_the_union(self) -> None:
        assert (
            ownership_hint(_Config(assignee_filter="coordinare-bot", include_unassigned=True))
            == "coordinare-bot + unassigned"
        )

    def test_unassigned_alone_is_named(self) -> None:
        """The regression: this rendered empty while the gate was excluding cards.

        The App-auth shape. Gating the hint on ``assignee_filter`` left the one
        configuration an App deployment can use looking like no policy at all.
        """
        assert ownership_hint(_Config(include_unassigned=True)) == "unassigned"

    def test_the_operators_own_spelling_survives(self) -> None:
        """Matching lowercases both sides; echoing the config back does not.

        A login that never matches is findable only if the operator can see the
        string they actually typed.
        """
        assert ownership_hint(_Config(assignee_filter="Coordinare-Bot")) == "Coordinare-Bot"

    def test_a_whitespace_login_narrows_nothing_and_says_nothing(self) -> None:
        assert ownership_hint(_Config(assignee_filter="   ")) == ""

    def test_a_whitespace_login_does_not_swallow_the_other_opt_in(self) -> None:
        assert ownership_hint(_Config(assignee_filter="  ", include_unassigned=True)) == "unassigned"

    def test_a_non_string_login_is_not_a_policy(self) -> None:
        assert ownership_hint(_Config(assignee_filter=123)) == ""

    def test_no_config_at_all(self) -> None:
        assert ownership_hint(None) == ""

    def test_the_hint_is_non_empty_exactly_when_the_gate_narrows(self) -> None:
        """The property that makes this hint trustworthy rather than decorative."""
        for filt in (None, "", "   ", "coordinare-bot", 123):
            for unassigned in (False, True):
                config = _Config(assignee_filter=filt, include_unassigned=unassigned)
                assert bool(ownership_hint(config)) is ownership_policy(config).active


class TestDashboardReadsOnlyTheHint:
    """Both render sites ask for the computed hint, not the raw fields.

    A Python test on ``ownership_hint`` cannot see the browser re-deriving the
    policy from ``assignee_filter``, which is exactly how the defect survived: the
    logic was correct in Python and stale in two lines of inlined JavaScript.
    """

    def test_neither_site_re_derives_the_policy(self) -> None:
        assert "s.assignee_filter ?" not in _DASHBOARD_HTML
        assert "s.assignee_filter\n" not in _DASHBOARD_HTML

    def test_both_sites_read_the_hint(self) -> None:
        """Named line by line: a count would pass with one site fixed twice."""
        assert (
            "var idleFilterText = s.ownership_hint ? esc(s.ownership_hint) : '';"
            in _DASHBOARD_HTML
        )
        assert "var filterHint = s.ownership_hint ? esc(s.ownership_hint) : '';" in _DASHBOARD_HTML
