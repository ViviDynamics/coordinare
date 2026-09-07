"""Budget configuration for the closer workflow (spec 172).

Loads budgets from environment variables with defaults.
"""

from __future__ import annotations


class CloserBudgets:
    """Budget configuration for closer workflow execution."""

    def __init__(
        self,
        max_threads_per_call: int = 20,
        max_pages: int = 5,
    ):
        if max_threads_per_call <= 0:
            raise ValueError("max_threads_per_call must be positive")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self.max_threads_per_call = max_threads_per_call
        self.max_pages = max_pages

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> CloserBudgets:
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

        max_threads_per_call = parse_int_strict("CLOSER_MAX_THREADS_PER_CALL", 20, min_val=1)
        max_pages = parse_int_strict("CLOSER_MAX_PAGES", 5, min_val=1)

        return cls(
            max_threads_per_call=max_threads_per_call,
            max_pages=max_pages,
        )


__all__ = ["CloserBudgets"]
