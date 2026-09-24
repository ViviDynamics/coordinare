"""Budgets for the implementer workflow (spec 167).

Per-turn timeouts, repair caps, quality commands, and CI wait budgets.
Read from workflow_env with sensible defaults and invalid values ignored.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ImplementerBudgets:
    """Implementer workflow execution budgets (FR-016).

    Attributes:
        turn_timeout_s: Wall clock per harness turn (20 min default).
        tests_reprompts: Reprompts for vacuous tests (1 default).
        impl_attempts: Implementation attempts per milestone (3 default).
        quality_repairs: Repairs per quality failure (2 default).
        ci_repairs: Repairs per CI failure (3 default).
        ci_wait_s: Total CI polling wait budget (30 min default).
        ci_no_checks_floor_s: Wall clock to wait for the first check run to
            appear before declaring the repo CI-less (60s default, 0 disables).
        test_timeout_s: Wall clock for one test-command run (10 min default).
        quality_commands: Newline-separated quality commands after lint.

    Read beside these by plan.select_lane: IMPL_DEFAULT_KIND, the lane a card
    takes when no blueprint names a work kind (default feature).
    """

    turn_timeout_s: int = 1200
    tests_reprompts: int = 1
    impl_attempts: int = 3
    quality_repairs: int = 2
    ci_repairs: int = 3
    ci_wait_s: int = 1800
    ci_no_checks_floor_s: int = 60
    #: Wall clock for ONE test-command run. 379: a suite slower than this can
    #: never complete, so the baseline and the local gate are killed every time
    #: and the card blocks with 'no test results were produced'.
    test_timeout_s: int = 600
    quality_commands: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, workflow_env: dict[str, str] | None) -> ImplementerBudgets:
        """Read budgets from workflow_env with defaults.

        Environment variables (case-sensitive):
          IMPL_TURN_TIMEOUT_S: turn_timeout_s
          IMPL_TESTS_REPROMPTS: tests_reprompts
          IMPL_ATTEMPTS: impl_attempts
          IMPL_QUALITY_REPAIRS: quality_repairs
          IMPL_CI_REPAIRS: ci_repairs
          IMPL_CI_WAIT_S: ci_wait_s
          IMPL_CI_NO_CHECKS_FLOOR_S: ci_no_checks_floor_s
          IMPL_TEST_TIMEOUT_S: test_timeout_s
          QUALITY_COMMANDS: newline-separated commands

        Invalid values (non-numeric, non-positive) are logged and ignored,
        falling back to the default. QUALITY_COMMANDS is split on newlines,
        stripped, and empty lines dropped.

        Args:
            workflow_env: Dispatch payload workflow_env dict, or None.

        Returns:
            ImplementerBudgets with read or default values.
        """
        env = workflow_env or {}

        def _read_seconds(key: str, default: int) -> int:
            raw = env.get(key)
            if not raw:
                return default
            try:
                value = int(raw.strip())
                # zero is a real choice for the CI wait (poll once, never wait);
                # every other cap needs at least one attempt
                if value < 0 or (value == 0 and key not in {"IMPL_CI_WAIT_S", "IMPL_CI_NO_CHECKS_FLOOR_S"}):
                    log.warning("impl_budget.invalid_value", key=key, raw=raw, default=default)
                    return default
                return value
            except ValueError:
                log.warning("impl_budget.invalid_value", key=key, raw=raw, default=default)
                return default

        commands_raw = env.get("QUALITY_COMMANDS", "")
        quality_commands = tuple(
            line.strip()
            for line in commands_raw.split("\n")
            if line.strip()
        )

        return cls(
            turn_timeout_s=_read_seconds("IMPL_TURN_TIMEOUT_S", 1200),
            tests_reprompts=_read_seconds("IMPL_TESTS_REPROMPTS", 1),
            impl_attempts=_read_seconds("IMPL_ATTEMPTS", 3),
            quality_repairs=_read_seconds("IMPL_QUALITY_REPAIRS", 2),
            ci_repairs=_read_seconds("IMPL_CI_REPAIRS", 3),
            ci_wait_s=_read_seconds("IMPL_CI_WAIT_S", 1800),
            ci_no_checks_floor_s=_read_seconds("IMPL_CI_NO_CHECKS_FLOOR_S", 60),
            test_timeout_s=_read_seconds("IMPL_TEST_TIMEOUT_S", 600),
            quality_commands=quality_commands,
        )
