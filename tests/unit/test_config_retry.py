"""Unit tests for ServiceRetryConfig defaults and _retry_config_from wiring."""
from __future__ import annotations

from coordinare.config import ServiceRetryConfig


def test_wait_exp_base_default_is_fibonacci() -> None:
    """Default wait_exp_base uses the golden ratio (fibonacci growth)."""
    cfg = ServiceRetryConfig()
    assert cfg.wait_exp_base == 1.618


def test_wait_exp_base_wired_to_retry_config() -> None:
    """_retry_config_from propagates wait_exp_base to the RetryConfig dataclass."""
    from coordinare.__main__ import _retry_config_from

    src = ServiceRetryConfig(wait_exp_base=1.5)
    result = _retry_config_from(src)
    assert result.wait_exp_base == 1.5
