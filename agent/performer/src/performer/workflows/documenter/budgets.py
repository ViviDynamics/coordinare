"""Budgets for documenter workflow (spec 171)."""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DocumenterBudgets"]


@dataclass
class DocumenterBudgets:
    """Budget configuration for documenter workflow."""

    plan_cap: int = 8
    gather_max_commands: int = 6
    gather_max_output_chars: int = 4000
    page_max_chars: int = 12000

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> DocumenterBudgets:
        """Parse budgets from environment variables.

        Environment variables:
        - DOC_PLAN_CAP (default 8, max 8)
        - DOC_GATHER_MAX_COMMANDS (default 6)
        - DOC_GATHER_MAX_OUTPUT_CHARS (default 4000)
        - DOC_PAGE_MAX_CHARS (default 12000)

        Non-integer or negative values fall back to defaults.

        Args:
            env: Environment dict (or None).

        Returns:
            DocumenterBudgets instance.
        """
        if not env:
            env = {}

        def parse_int(key: str, default: int, max_val: int | None = None) -> int:
            try:
                val = int(env.get(key, default))
                if val <= 0:
                    return default
                if max_val is not None and val > max_val:
                    return max_val
                return val
            except (ValueError, TypeError):
                return default

        plan_cap = parse_int("DOC_PLAN_CAP", 8, max_val=8)
        gather_max_commands = parse_int("DOC_GATHER_MAX_COMMANDS", 6)
        gather_max_output_chars = parse_int("DOC_GATHER_MAX_OUTPUT_CHARS", 4000)
        page_max_chars = parse_int("DOC_PAGE_MAX_CHARS", 12000)

        return cls(
            plan_cap=plan_cap,
            gather_max_commands=gather_max_commands,
            gather_max_output_chars=gather_max_output_chars,
            page_max_chars=page_max_chars,
        )
