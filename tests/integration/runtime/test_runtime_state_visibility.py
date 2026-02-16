from __future__ import annotations

from pathlib import Path

from coordinare.lib.runtime_events import build_runtime_event


def test_runtime_event_state_context_is_observable() -> None:
    event = build_runtime_event(
        category="state_change",
        message="state transition detected",
        previous_phase="idle",
        current_phase="running",
    )

    assert event["category"] == "state_change"
    assert event["previous_phase"] == "idle"
    assert event["current_phase"] == "running"


def test_contracts_describe_same_state_visibility_categories() -> None:
    shell_contract = Path("specs/002-docker-cli-output/contracts/shell-runtime-contract.md").read_text()
    compose_contract = Path("specs/002-docker-cli-output/contracts/compose-runtime-contract.md").read_text()

    for keyword in ("startup", "activity", "heartbeat", "failure", "shutdown"):
        assert keyword in shell_contract
        assert keyword in compose_contract
