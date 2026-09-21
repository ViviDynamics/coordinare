"""Per-role performer slot management (048).

Tracks active performer instances per role, enforces max_concurrency
limits, and provides utilization data for the dashboard.  The
SlotManager is stateful (``active_slots`` mutated via ``acquire`` /
``release``), but ``sync_from_sessions`` reconciles it against the
authoritative ``active_sessions`` dict each cycle to recover from
restarts or missed release calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from coordinare.lifecycle import ROLE_TO_STAGE, SINGLETON_STAGES

logger = structlog.get_logger(__name__)


class SlotState(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


@dataclass
class PerformerSlot:
    """One active performer instance serving a card."""

    role: str
    card_id: str
    session_id: str = ""
    service_index: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: SlotState = SlotState.RUNNING


@dataclass
class RolePool:
    """Tracks slots for a single performer role."""

    role: str
    max_concurrency: int = 1
    services: list[Any] = field(default_factory=list)
    active_slots: dict[str, PerformerSlot] = field(default_factory=dict)

    @property
    def active_count(self) -> int:
        return len(self.active_slots)

    @property
    def is_at_capacity(self) -> bool:
        if self.max_concurrency <= 0:
            return False  # disabled role, not "at capacity" — let dispatch skip it
        return self.active_count >= self.max_concurrency

    def free_service_index(self) -> int | None:
        """Return the index of a free service, or None if all occupied."""
        used = {s.service_index for s in self.active_slots.values()}
        for i in range(len(self.services)):
            if i not in used:
                return i
        return None


class SlotManager:
    """Orchestrates per-role performer slot allocation."""

    def __init__(self) -> None:
        self.pools: dict[str, RolePool] = {}

    def register_pool(
        self,
        stage: str,
        services: list[Any],
        max_concurrency: int,
    ) -> None:
        """Register a role pool with its services and concurrency limit."""
        # Clamp singletons
        if stage in SINGLETON_STAGES and max_concurrency > 1:
            logger.warning(
                "slot_manager.singleton_clamped",
                stage=stage,
                configured=max_concurrency,
                clamped_to=1,
            )
            max_concurrency = 1
        # Clamp to len(services) — can't use more slots than transports.
        # Mirror the hot-reload code path: log so operators can grep for
        # the cap when a configured max_concurrency isn't taking effect.
        if services and max_concurrency > len(services):
            logger.warning(
                "slot_manager.register.capped_by_services",
                stage=stage,
                requested=max_concurrency,
                available=len(services),
                hint="add more transport instances to honor the configured max_concurrency",
            )
            max_concurrency = len(services)
        self.pools[stage] = RolePool(
            role=stage,
            max_concurrency=max_concurrency,
            services=services,
        )

    def acquire(
        self,
        stage: str,
        card_id: str,
        *,
        config: Any = None,
    ) -> Any | None:
        """Allocate a free slot for a card.

        Returns an AgentService if a slot is free, or None if at capacity.
        Reads ``max_concurrency`` from config if provided (hot-reload support).
        """
        pool = self.pools.get(stage)
        if pool is None:
            return None

        # Hot-reload: update max_concurrency from config if available
        if config is not None:
            role_name = None
            for r, s in ROLE_TO_STAGE.items():
                if s == stage:
                    role_name = r
                    break
            if role_name is not None:
                performers_cfg = getattr(config, "performers", None)
                if performers_cfg is not None:
                    # Prefer resolved_role() so ``performers.default`` propagates;
                    # fall back to raw attribute access for ducktyped test configs.
                    if hasattr(performers_cfg, "resolved_role"):
                        role_cfg = performers_cfg.resolved_role(role_name)
                    else:
                        role_cfg = getattr(performers_cfg, role_name, None)
                    if role_cfg is not None:
                        new_max = getattr(role_cfg, "max_concurrency", 1)
                        if stage in SINGLETON_STAGES:
                            new_max = min(new_max, 1)
                        # Clamp to len(services) — can't use more slots than
                        # we have transport instances (created at startup).
                        # If the operator bumped max_concurrency in config but
                        # we can't honor it without rebuilding transports, log
                        # once so it's obvious a restart is required.
                        if pool.services and new_max > len(pool.services):
                            if not getattr(pool, "_capped_warned", False):
                                logger.warning(
                                    "slot_manager.hot_reload.capped_by_services",
                                    stage=stage,
                                    requested=new_max,
                                    available=len(pool.services),
                                    hint="restart coordinare to grow the transport pool",
                                )
                                pool._capped_warned = True  # type: ignore[attr-defined]
                            new_max = len(pool.services)
                        pool.max_concurrency = new_max

        if pool.max_concurrency <= 0:
            return None

        # Already allocated?
        if card_id in pool.active_slots:
            slot = pool.active_slots[card_id]
            if slot.service_index < len(pool.services):
                return pool.services[slot.service_index]
            return pool.services[0] if pool.services else None

        if pool.is_at_capacity:
            return None

        idx = pool.free_service_index()
        if idx is None:
            # More active than services (shouldn't happen with correct setup)
            return None

        pool.active_slots[card_id] = PerformerSlot(
            role=stage,
            card_id=card_id,
            service_index=idx,
        )
        logger.info(
            "slot_manager.acquired",
            stage=stage,
            card_id=card_id,
            service_index=idx,
            active=pool.active_count,
            max=pool.max_concurrency,
        )
        return pool.services[idx]

    def release(self, stage: str, card_id: str) -> None:
        """Free the slot after performer completes or crashes."""
        pool = self.pools.get(stage)
        if pool is None:
            return
        slot = pool.active_slots.pop(card_id, None)
        if slot is not None:
            logger.info(
                "slot_manager.released",
                stage=stage,
                card_id=card_id,
                active=pool.active_count,
                max=pool.max_concurrency,
            )

    def active_count(self, stage: str) -> int:
        pool = self.pools.get(stage)
        return pool.active_count if pool else 0

    def is_at_capacity(self, stage: str) -> bool:
        pool = self.pools.get(stage)
        if pool is None:
            return False  # unknown/disabled stage → not "at capacity" (let dispatch skip it)
        return pool.is_at_capacity

    def sync_from_sessions(self, active_sessions: dict[str, Any]) -> None:
        """Rebuild active slots from session phases.

        Frees slots for sessions that completed or crashed since last sync.
        """
        # Build set of (stage, card_id) that are currently active performers
        active_performer_cards: set[tuple[str, str]] = set()
        for card_id, session in active_sessions.items():
            if not isinstance(session, dict):
                continue
            phase = session.get("phase", "")
            stage = session.get("performer_stage", "")
            if phase == "monitoring_performer" and stage:
                active_performer_cards.add((stage, card_id))

        # Free slots that are no longer in monitoring_performer
        for stage, pool in self.pools.items():
            stale = [
                cid for cid in pool.active_slots
                if (stage, cid) not in active_performer_cards
            ]
            for cid in stale:
                self.release(stage, cid)

        # Add missing slots for sessions that are in monitoring_performer
        # but don't have a slot (e.g. after a restart where the SlotManager
        # was reconstructed but active_sessions were restored from state).
        for stage, card_id in active_performer_cards:
            slot_pool = self.pools.get(stage)
            if slot_pool is not None and card_id not in slot_pool.active_slots:
                idx = slot_pool.free_service_index()
                if idx is not None:
                    slot_pool.active_slots[card_id] = PerformerSlot(
                        role=stage,
                        card_id=card_id,
                        service_index=idx,
                    )

    def utilization(
        self,
        queued_by_stage: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Per-role utilization data for the dashboard.

        ``queued_by_stage`` maps stage name → number of cards waiting in
        the ``dispatching`` phase for that stage.  Typically derived by
        the caller from ``active_sessions``.
        """
        queued = queued_by_stage or {}
        result: list[dict[str, Any]] = []
        for stage, pool in sorted(self.pools.items()):
            result.append({
                "role": stage,
                "active": pool.active_count,
                "max": pool.max_concurrency,
                "queued": queued.get(stage, 0),
            })
        return result
