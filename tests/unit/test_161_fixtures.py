"""Spec 161 — synthetic run-artifact builders shared by the 161 tests.

T002 (foundational): builders for RunArtifact / CardOutcome / PersonaDispatch carrying
arbitrary terminal markers, roles and backends.

Note on T003: there is deliberately no "find an existing run.json" fixture here. Verified
during planning that the tree contains no `runs/` directory and no `run.json` anywhere, so
a discovery-based fixture would silently no-op and quietly assert nothing. Artifacts are
therefore constructed explicitly, which is also what makes US1 runnable with no inference
host.
"""

from __future__ import annotations

from datetime import UTC, datetime

from coordinare.bench.artifact import (
    CardOutcome,
    ConfigFingerprint,
    PersonaDispatch,
    RunArtifact,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def dispatch(
    role: str,
    backend: str,
    marker: str | None,
    *,
    stage: str = "implementing",
    status: str = "succeeded",
    seconds: float | None = 1.0,
    tokens: int | None = 100,
    session_id: str | None = None,
) -> PersonaDispatch:
    """One dispatch row.

    `status` is independent of `marker` on purpose: the classifier must ignore it.
    """
    return PersonaDispatch(
        stage=stage,
        role=role,
        backend=backend,
        status=status,  # type: ignore[arg-type]
        terminal_marker=marker,
        seconds=seconds,
        tokens_processed=tokens,
        session_id=session_id,
    )


def card(card_id: str, dispatches: list[PersonaDispatch], *, final_state: str = "merged") -> CardOutcome:
    return CardOutcome(
        card_id=card_id,
        final_state=final_state,  # type: ignore[arg-type]
        dispatches=dispatches,
    )


def artifact(
    cards: list[CardOutcome],
    *,
    run_id: str = "run-1",
    wall_clock_seconds: float = 10.0,
) -> RunArtifact:
    return RunArtifact(
        run_id=run_id,
        started_at=NOW,
        finished_at=NOW,
        wall_clock_seconds=wall_clock_seconds,
        config_fingerprint=ConfigFingerprint(hash="abc123", source_path="config.yaml"),
        cards=cards,
    )


def simple_artifact(
    rows: list[tuple[str, str, str | None]],
    *,
    run_id: str = "run-1",
) -> RunArtifact:
    """Build a one-card artifact from `(role, backend, marker)` triples."""
    return artifact(
        [card("c1", [dispatch(role, backend, marker) for role, backend, marker in rows])],
        run_id=run_id,
    )


# --- the builders are themselves worth pinning ----------------------------------------


def test_builders_produce_a_schema_valid_artifact() -> None:
    """A malformed builder would make every downstream 161 test meaningless."""
    art = simple_artifact([("reviewer", "openclaw", "approved")])
    # Round-trips against its own schema, the guard RunArtifact already enforces.
    assert RunArtifact.model_validate_json(art.to_validated_json()).run_id == "run-1"


def test_status_and_marker_are_independent_in_the_builder() -> None:
    """The builder must be able to express the misleading pair the classifier ignores."""
    d = dispatch("reviewer", "openclaw", "changes_requested", status="failed")
    assert d.status == "failed"
    assert d.terminal_marker == "changes_requested"
