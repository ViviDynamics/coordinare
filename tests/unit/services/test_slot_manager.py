"""Unit tests for the performer slot manager (048)."""
from __future__ import annotations

from unittest.mock import MagicMock

from coordinare.services.slot_manager import SlotManager


def _mock_services(n: int = 3) -> list:
    """Create N mock AgentService instances."""
    return [MagicMock(name=f"service_{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# T011: SlotManager.acquire tests
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_free_slot_available(self) -> None:
        sm = SlotManager()
        services = _mock_services(2)
        sm.register_pool("implementing", services, max_concurrency=2)

        svc = sm.acquire("implementing", "CARD_A")

        assert svc is not None
        assert svc in services
        assert sm.active_count("implementing") == 1

    def test_at_capacity_returns_none(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")

        result = sm.acquire("implementing", "CARD_B")

        assert result is None
        assert sm.active_count("implementing") == 1

    def test_singleton_clamped_to_1(self) -> None:
        sm = SlotManager()
        sm.register_pool("assessing", _mock_services(5), max_concurrency=5)

        # Singleton should be clamped to 1
        pool = sm.pools["assessing"]
        assert pool.max_concurrency == 1

        sm.acquire("assessing", "CARD_A")
        result = sm.acquire("assessing", "CARD_B")
        assert result is None

    def test_closing_review_singleton_clamped(self) -> None:
        """Both SINGLETON_STAGES entries are enforced — not just assessing."""
        sm = SlotManager()
        sm.register_pool("closing_review", _mock_services(3), max_concurrency=3)

        pool = sm.pools["closing_review"]
        assert pool.max_concurrency == 1

    def test_max_concurrency_zero_returns_none(self) -> None:
        sm = SlotManager()
        sm.register_pool("security", _mock_services(1), max_concurrency=0)

        result = sm.acquire("security", "CARD_A")
        assert result is None

    def test_unknown_stage_returns_none(self) -> None:
        sm = SlotManager()
        assert sm.acquire("nonexistent", "CARD_A") is None

    def test_already_allocated_returns_same_service(self) -> None:
        sm = SlotManager()
        services = _mock_services(2)
        sm.register_pool("implementing", services, max_concurrency=2)

        svc1 = sm.acquire("implementing", "CARD_A")
        svc2 = sm.acquire("implementing", "CARD_A")

        assert svc1 is svc2

    def test_multiple_cards_get_different_services(self) -> None:
        sm = SlotManager()
        services = _mock_services(3)
        sm.register_pool("implementing", services, max_concurrency=3)

        svc_a = sm.acquire("implementing", "CARD_A")
        svc_b = sm.acquire("implementing", "CARD_B")

        assert svc_a is not svc_b
        assert svc_a in services
        assert svc_b in services


# ---------------------------------------------------------------------------
# T012: SlotManager.release tests
# ---------------------------------------------------------------------------


class TestRelease:
    def test_slot_freed(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")
        assert sm.active_count("implementing") == 1

        sm.release("implementing", "CARD_A")

        assert sm.active_count("implementing") == 0

    def test_release_unknown_card_is_noop(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)

        sm.release("implementing", "UNKNOWN")  # no error

        assert sm.active_count("implementing") == 0

    def test_release_unknown_stage_is_noop(self) -> None:
        sm = SlotManager()
        sm.release("nonexistent", "CARD_A")  # no error

    def test_freed_slot_available_for_next_card(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")
        assert sm.acquire("implementing", "CARD_B") is None

        sm.release("implementing", "CARD_A")

        result = sm.acquire("implementing", "CARD_B")
        assert result is not None


# ---------------------------------------------------------------------------
# T013: SlotManager.sync_from_sessions tests
# ---------------------------------------------------------------------------


class TestSyncFromSessions:
    def test_completed_sessions_freed(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(2), max_concurrency=2)
        sm.acquire("implementing", "CARD_A")
        sm.acquire("implementing", "CARD_B")
        assert sm.active_count("implementing") == 2

        # CARD_A completed (phase=idle), CARD_B still active
        sessions = {
            "CARD_A": {"phase": "idle", "performer_stage": "implementing"},
            "CARD_B": {"phase": "monitoring_performer", "performer_stage": "implementing"},
        }
        sm.sync_from_sessions(sessions)

        assert sm.active_count("implementing") == 1
        assert "CARD_B" in sm.pools["implementing"].active_slots

    def test_crashed_sessions_freed(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")

        # Session removed entirely (crash/timeout)
        sm.sync_from_sessions({})

        assert sm.active_count("implementing") == 0

    def test_active_sessions_retained(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")

        sessions = {
            "CARD_A": {"phase": "monitoring_performer", "performer_stage": "implementing"},
        }
        sm.sync_from_sessions(sessions)

        assert sm.active_count("implementing") == 1


# ---------------------------------------------------------------------------
# T014: SlotManager.utilization tests
# ---------------------------------------------------------------------------


class TestUtilization:
    def test_mixed_roles(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(3), max_concurrency=3)
        sm.register_pool("reviewing", _mock_services(1), max_concurrency=1)
        sm.acquire("implementing", "CARD_A")
        sm.acquire("implementing", "CARD_B")
        sm.acquire("reviewing", "CARD_C")

        util = sm.utilization()

        impl = next(u for u in util if u["role"] == "implementing")
        assert impl["active"] == 2
        assert impl["max"] == 3

        rev = next(u for u in util if u["role"] == "reviewing")
        assert rev["active"] == 1
        assert rev["max"] == 1

    def test_empty_pools(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(2), max_concurrency=2)

        util = sm.utilization()

        assert len(util) == 1
        assert util[0]["active"] == 0
        assert util[0]["max"] == 2

    def test_is_at_capacity(self) -> None:
        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)

        assert not sm.is_at_capacity("implementing")
        sm.acquire("implementing", "CARD_A")
        assert sm.is_at_capacity("implementing")


# ---------------------------------------------------------------------------
# Coverage: hot-reload, free_service_index edge cases
# ---------------------------------------------------------------------------


class TestHotReloadAndEdgeCases:
    def test_acquire_with_config_hot_reload(self) -> None:
        """acquire reads max_concurrency from config when provided."""
        from types import SimpleNamespace

        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(3), max_concurrency=1)

        # Config says max_concurrency=3 for implementer
        config = SimpleNamespace(
            performers=SimpleNamespace(
                implementer=SimpleNamespace(max_concurrency=3),
            ),
        )
        sm.acquire("implementing", "CARD_A", config=config)
        svc_b = sm.acquire("implementing", "CARD_B", config=config)

        # Should succeed because config overrides the initial 1 to 3
        assert svc_b is not None
        assert sm.active_count("implementing") == 2

    def test_hot_reload_honors_performers_default(self) -> None:
        """performers.default.max_concurrency propagates when role override unset."""
        from types import SimpleNamespace

        from coordinare.config import PerformerRoleConfig, PerformersConfig

        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(3), max_concurrency=1)

        # Only ``default`` sets max_concurrency=3; implementer has no override.
        performers = PerformersConfig(
            default=PerformerRoleConfig(max_concurrency=3),
            implementer=PerformerRoleConfig(),
        )
        config = SimpleNamespace(performers=performers)

        sm.acquire("implementing", "CARD_A", config=config)
        svc_b = sm.acquire("implementing", "CARD_B", config=config)

        assert svc_b is not None
        assert sm.active_count("implementing") == 2

    def test_hot_reload_warns_when_capped_by_services(self) -> None:
        """Bumping max_concurrency above len(services) flags the pool once."""
        from types import SimpleNamespace

        sm = SlotManager()
        sm.register_pool("implementing", _mock_services(1), max_concurrency=1)

        config = SimpleNamespace(
            performers=SimpleNamespace(
                implementer=SimpleNamespace(max_concurrency=5),
            ),
        )
        sm.acquire("implementing", "CARD_A", config=config)

        # Effective max stays at len(services)=1; pool is marked so we don't
        # spam the same warning on every subsequent acquire().
        pool = sm.pools["implementing"]
        assert pool.max_concurrency == 1
        assert getattr(pool, "_capped_warned", False) is True

    def test_singleton_clamped_even_with_config_override(self) -> None:
        from types import SimpleNamespace

        sm = SlotManager()
        sm.register_pool("assessing", _mock_services(3), max_concurrency=1)

        config = SimpleNamespace(
            performers=SimpleNamespace(
                assessor=SimpleNamespace(max_concurrency=5),
            ),
        )
        sm.acquire("assessing", "CARD_A", config=config)
        result = sm.acquire("assessing", "CARD_B", config=config)
        assert result is None  # still clamped to 1

    def test_free_service_index_all_occupied(self) -> None:
        """When all services are occupied, free_service_index returns None."""
        from coordinare.services.slot_manager import PerformerSlot, RolePool
        pool = RolePool(
            role="implementing",
            max_concurrency=2,
            services=_mock_services(2),
        )
        pool.active_slots["A"] = PerformerSlot(role="implementing", card_id="A", service_index=0)
        pool.active_slots["B"] = PerformerSlot(role="implementing", card_id="B", service_index=1)
        assert pool.free_service_index() is None

    def test_is_at_capacity_unknown_stage_returns_false(self) -> None:
        """Unknown stage is NOT at capacity — dispatch should skip it via
        the 'no service configured' path, not loop forever retrying."""
        sm = SlotManager()
        assert sm.is_at_capacity("nonexistent") is False

    def test_is_at_capacity_disabled_role_returns_false(self) -> None:
        """max_concurrency=0 (disabled) is not 'at capacity' — dispatch
        should skip, not queue forever."""
        sm = SlotManager()
        sm.register_pool("security", _mock_services(1), max_concurrency=0)
        assert sm.is_at_capacity("security") is False
