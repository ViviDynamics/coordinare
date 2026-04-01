"""BackendAdapter protocol and BackendStatus value object."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, runtime_checkable

from typing import Protocol

if TYPE_CHECKING:
    from performer.models import BackendEvent, Score, Stand


@dataclass
class BackendStatus:
    """Current state reported by a BackendAdapter."""

    state: Literal["working", "blocked", "done", "error"] = "working"
    questions: list[str] = field(default_factory=list)
    error_reason: str | None = None
    tokens_processed: int | None = None
    progress: str | None = None
    output: str | None = None  # 020: AI-generated content (e.g. architecture plan)


@runtime_checkable
class BackendAdapter(Protocol):
    """Strategy interface implemented by each AI coding backend."""

    async def start(self, stand: "Stand", score: "Score", *, model: str | None = None) -> None:
        """Launch the backend in *stand* with the task from *score*."""
        ...

    def get_status(self) -> BackendStatus:
        """Non-blocking: return current backend state."""
        ...

    def drain_events(self) -> list[BackendEvent]:
        """Non-blocking: return all buffered events since last call and clear buffer."""
        ...

    async def relay_feedback(self, feedback: str) -> None:
        """Deliver human feedback to the running backend."""
        ...

    async def stop(self) -> None:
        """Terminate the backend process tree."""
        ...
