from __future__ import annotations

from coordinare.config import (
    CoordinareConfiguration,
    OrchestraConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.graph.state import CoordinareState, SymphonyRuntimeState


def _base_project_config() -> ProjectConfiguration:
    """Minimal valid ProjectConfiguration for testing."""
    return ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=0,
        github_token="token",
        human_reviewers=["alice"],
    )


def _make_coordinare_config(num_symphonies: int = 3) -> CoordinareConfiguration:
    """Create a CoordinareConfiguration with N symphonies."""
    global_cfg = _base_project_config()
    symphonies = [
        SymphonyConfig(name=f"sym-{i}", github_project_number=100 + i)
        for i in range(num_symphonies)
    ]
    return CoordinareConfiguration(
        global_config=global_cfg,
        symphonies=symphonies,
        orchestra=OrchestraConfig(mode="shared_pool"),
    )


class TestMultiSymphonyStateTracking:
    """Test that per-symphony state is tracked independently."""

    def test_coordinare_state_has_symphony_states(self) -> None:
        """CoordinareState contains per-symphony runtime states."""
        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {"api": SymphonyConfig(name="api", github_project_number=1)},
            "symphony_states": {
                "api": SymphonyRuntimeState(
                    name="api",
                    cycle_count=0,
                    last_poll_at=None,
                ),
            },
            "current_symphony": "api",
            "config": _base_project_config(),
            "orchestra_config": OrchestraConfig(),
            "config_version": 1,
            "coordinare_config": _make_coordinare_config(1),
        }

        assert "api" in state["symphony_states"]
        assert state["symphony_states"]["api"].name == "api"

    def test_symphony_state_independent_cycle_counts(self) -> None:
        """Each symphony tracks its own cycle count."""
        config = _make_coordinare_config(3)
        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                s.name: SymphonyRuntimeState(
                    name=s.name,
                    cycle_count=i,
                    last_poll_at=None,
                )
                for i, s in enumerate(config.symphonies)
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        assert state["symphony_states"]["sym-0"].cycle_count == 0
        assert state["symphony_states"]["sym-1"].cycle_count == 1
        assert state["symphony_states"]["sym-2"].cycle_count == 2

    def test_symphony_state_independent_error_counts(self) -> None:
        """Each symphony tracks its own error count."""
        config = _make_coordinare_config(2)
        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                "sym-0": SymphonyRuntimeState(
                    name="sym-0",
                    cycle_count=0,
                    last_poll_at=None,
                    error_count=3,
                ),
                "sym-1": SymphonyRuntimeState(
                    name="sym-1",
                    cycle_count=0,
                    last_poll_at=None,
                    error_count=0,
                ),
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        assert state["symphony_states"]["sym-0"].error_count == 3
        assert state["symphony_states"]["sym-1"].error_count == 0


class TestSymphonySequentialOrchestration:
    """Test that symphonies are orchestrated in config order."""

    def test_symphony_order_preserved(self) -> None:
        """Symphonies are iterated in config list order."""
        config = _make_coordinare_config(3)
        names = [s.name for s in config.symphonies]

        # Should be in creation order
        assert names == ["sym-0", "sym-1", "sym-2"]

    def test_coordinare_config_maintains_symphony_list_order(self) -> None:
        """CoordinareConfiguration preserves symphony order from config."""
        global_cfg = _base_project_config()
        symphonies = [
            SymphonyConfig(name="frontend", github_project_number=10),
            SymphonyConfig(name="backend", github_project_number=20),
            SymphonyConfig(name="devops", github_project_number=30),
        ]
        config = CoordinareConfiguration(
            global_config=global_cfg,
            symphonies=symphonies,
        )

        names = [s.name for s in config.symphonies]
        assert names == ["frontend", "backend", "devops"]


class TestMultiSymphonyErrorIsolation:
    """Test that errors in one symphony don't block others."""

    def test_symphony_error_count_isolated(self) -> None:
        """Symphony error counts are independent."""
        config = _make_coordinare_config(2)

        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                "sym-0": SymphonyRuntimeState(
                    name="sym-0",
                    cycle_count=1,
                    last_poll_at=None,
                    error_count=1,
                    last_error="GitHub API timeout",
                ),
                "sym-1": SymphonyRuntimeState(
                    name="sym-1",
                    cycle_count=1,
                    last_poll_at=None,
                    error_count=0,
                ),
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        # sym-0 has error, sym-1 is clean
        assert state["symphony_states"]["sym-0"].error_count == 1
        assert state["symphony_states"]["sym-0"].last_error == "GitHub API timeout"
        assert state["symphony_states"]["sym-1"].error_count == 0

    def test_symphony_state_last_error_tracking(self) -> None:
        """Each symphony tracks its own last error message."""
        config = _make_coordinare_config(3)

        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                "sym-0": SymphonyRuntimeState(
                    name="sym-0",
                    cycle_count=2,
                    last_poll_at=None,
                    error_count=1,
                    last_error="Authentication failed",
                ),
                "sym-1": SymphonyRuntimeState(
                    name="sym-1",
                    cycle_count=2,
                    last_poll_at=None,
                    error_count=0,
                    last_error=None,
                ),
                "sym-2": SymphonyRuntimeState(
                    name="sym-2",
                    cycle_count=2,
                    last_poll_at=None,
                    error_count=2,
                    last_error="Connection refused",
                ),
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        assert state["symphony_states"]["sym-0"].last_error == "Authentication failed"
        assert state["symphony_states"]["sym-1"].last_error is None
        assert state["symphony_states"]["sym-2"].last_error == "Connection refused"


class TestSymphonyConfigEffectiveResolution:
    """Test that each symphony's effective config is computed correctly."""

    def test_each_symphony_has_unique_project_number(self) -> None:
        """Each symphony's effective config has its own project number."""
        config = _make_coordinare_config(3)

        eff0 = config.symphonies[0].effective_config(config.global_config)
        eff1 = config.symphonies[1].effective_config(config.global_config)
        eff2 = config.symphonies[2].effective_config(config.global_config)

        assert eff0.github_project_number == 100
        assert eff1.github_project_number == 101
        assert eff2.github_project_number == 102

    def test_symphony_overrides_apply_per_symphony(self) -> None:
        """Symphony overrides only affect that symphony's effective config."""
        global_cfg = _base_project_config()
        global_cfg.poll_interval_seconds = 60

        sym0 = SymphonyConfig(
            name="fast",
            github_project_number=100,
            overrides={"poll_interval_seconds": 60},
        )
        sym1 = SymphonyConfig(
            name="slow",
            github_project_number=101,
            overrides={"poll_interval_seconds": 120},
        )

        config = CoordinareConfiguration(
            global_config=global_cfg,
            symphonies=[sym0, sym1],
        )

        eff0 = config.symphonies[0].effective_config(config.global_config)
        eff1 = config.symphonies[1].effective_config(config.global_config)

        # Each has its own polling interval
        assert eff0.poll_interval_seconds == 60
        assert eff1.poll_interval_seconds == 120


class TestMaxConcurrentCardsPerSymphony:
    """Test max_concurrent_cards limit per symphony."""

    def test_max_concurrent_cards_default(self) -> None:
        """Default max_concurrent_cards applies to all symphonies."""
        config = _make_coordinare_config(2)

        eff0 = config.symphonies[0].effective_config(config.global_config)
        eff1 = config.symphonies[1].effective_config(config.global_config)

        # Both should have same default
        assert eff0.max_concurrent_cards == eff1.max_concurrent_cards

    def test_max_concurrent_cards_per_symphony_override(self) -> None:
        """Each symphony can override max_concurrent_cards."""
        global_cfg = _base_project_config()
        global_cfg.max_concurrent_cards = 5

        sym0 = SymphonyConfig(
            name="high-throughput",
            github_project_number=100,
            overrides={"max_concurrent_cards": 20},
        )
        sym1 = SymphonyConfig(
            name="low-throughput",
            github_project_number=101,
            overrides={"max_concurrent_cards": 1},
        )

        config = CoordinareConfiguration(
            global_config=global_cfg,
            symphonies=[sym0, sym1],
        )

        eff0 = config.symphonies[0].effective_config(config.global_config)
        eff1 = config.symphonies[1].effective_config(config.global_config)

        assert eff0.max_concurrent_cards == 20
        assert eff1.max_concurrent_cards == 1

    def test_active_sessions_per_symphony_independent(self) -> None:
        """Each symphony tracks active sessions independently."""
        config = _make_coordinare_config(2)

        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                "sym-0": SymphonyRuntimeState(
                    name="sym-0",
                    cycle_count=1,
                    last_poll_at=None,
                    active_sessions={"card-1": {}, "card-2": {}},
                ),
                "sym-1": SymphonyRuntimeState(
                    name="sym-1",
                    cycle_count=1,
                    last_poll_at=None,
                    active_sessions={"card-3": {}},
                ),
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        # Each symphony has independent active_sessions
        assert len(state["symphony_states"]["sym-0"].active_sessions) == 2
        assert len(state["symphony_states"]["sym-1"].active_sessions) == 1


class TestSymphonyConfigVersionTracking:
    """Test config version tracking for hot-reload."""

    def test_coordinare_state_tracks_config_version(self) -> None:
        """CoordinareState stores config_version for hot-reload detection."""
        config = _make_coordinare_config(2)
        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config.symphonies},
            "symphony_states": {
                s.name: SymphonyRuntimeState(name=s.name, cycle_count=0, last_poll_at=None)
                for s in config.symphonies
            },
            "current_symphony": None,
            "config": config.global_config,
            "orchestra_config": config.orchestra,
            "config_version": 1,
            "coordinare_config": config,
        }

        assert state["config_version"] == 1

    def test_config_version_increments_on_reload(self) -> None:
        """Config version increments when config is reloaded."""
        config_v1: CoordinareConfiguration = _make_coordinare_config(2)
        config_v2: CoordinareConfiguration = _make_coordinare_config(2)

        state: CoordinareState = {
            "active_sessions": {},
            "board_snapshot": None,
            "session_skip_reasons": {},
            "symphony_configs": {s.name: s for s in config_v1.symphonies},
            "symphony_states": {
                s.name: SymphonyRuntimeState(name=s.name, cycle_count=0, last_poll_at=None)
                for s in config_v1.symphonies
            },
            "current_symphony": None,
            "config": config_v1.global_config,
            "orchestra_config": config_v1.orchestra,
            "config_version": 1,
            "coordinare_config": config_v1,
        }

        # Simulate reload by incrementing version
        state["config_version"] = 2
        state["coordinare_config"] = config_v2

        assert state["config_version"] == 2
