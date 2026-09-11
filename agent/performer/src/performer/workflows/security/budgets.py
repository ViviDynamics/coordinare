"""Budget configuration for the security workflow (spec 170).

Loads budgets from environment variables with defaults.
"""

from __future__ import annotations

from performer.workflows.reviewer.budgets import SurveyBudget


class SecurityBudgets:
    """Budget configuration for security workflow execution."""

    def __init__(
        self,
        scan_timeout_s: int = 120,
        survey_max_commands: int = 12,
        survey_max_output_chars: int = 4000,
        max_findings: int = 30,
    ):
        if scan_timeout_s <= 0:
            raise ValueError("scan_timeout_s must be positive")
        self.scan_timeout_s = scan_timeout_s
        self.survey_max_commands = survey_max_commands
        self.survey_max_output_chars = survey_max_output_chars
        self.max_findings = min(max_findings, 30)  # Never exceed 30

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> SecurityBudgets:
        """Load budgets from environment variables with fallback defaults."""
        env = env or {}

        def parse_int_strict(key: str, default: int, min_val: int | None = None) -> int:
            """Parse an environment variable as an int, enforce min_val if set, raise on explicit invalid."""
            raw = env.get(key)
            if raw is None:
                return default
            try:
                val = int(raw)
            except ValueError:
                # Non-numeric value, return default
                return default

            if min_val is not None and val < min_val:
                raise ValueError(f"{key} must be >= {min_val}, got {val}")
            return val

        scan_timeout_s = parse_int_strict("SECURITY_SCAN_TIMEOUT_S", 120, min_val=1)
        survey_max_commands = parse_int_strict("SECURITY_SURVEY_MAX_COMMANDS", 12, min_val=1)
        survey_max_output_chars = parse_int_strict("SECURITY_SURVEY_MAX_OUTPUT_CHARS", 4000, min_val=1)
        max_findings = parse_int_strict("SECURITY_MAX_FINDINGS", 30, min_val=1)

        return cls(
            scan_timeout_s=scan_timeout_s,
            survey_max_commands=survey_max_commands,
            survey_max_output_chars=survey_max_output_chars,
            max_findings=max_findings,
        )

    def survey_budget(self) -> SurveyBudget:
        """Return the architect's survey budget."""
        return SurveyBudget(max_commands=self.survey_max_commands, max_output_chars=self.survey_max_output_chars)


__all__ = ["SecurityBudgets"]
