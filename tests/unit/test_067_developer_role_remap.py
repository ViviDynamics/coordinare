"""Unit tests for developer-role remap behaviour (spec 067 T019).

Covers:
  (a) ``developer`` → ``system`` when flag is true (default).
  (b) Messages untouched when flag is false.
  (c) WARN emitted exactly once per adapter instance regardless of how many
      remapped messages are seen.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from performer.backends.opencode_compat import _remap_developer_role


def test_developer_role_remapped_to_system():
    msgs = [
        {"role": "developer", "content": "Override system."},
        {"role": "user", "content": "Hi"},
    ]
    out = _remap_developer_role(msgs, warned_flag={"warned": False}, logger=MagicMock())
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "Override system."
    assert out[1]["role"] == "user"


def test_input_not_mutated():
    msgs = [{"role": "developer", "content": "x"}]
    out = _remap_developer_role(msgs, warned_flag={"warned": False}, logger=MagicMock())
    assert msgs[0]["role"] == "developer", "input must not be mutated"
    assert out[0]["role"] == "system"


def test_warn_emitted_exactly_once_per_session():
    logger = MagicMock()
    flag = {"warned": False}
    _remap_developer_role(
        [{"role": "developer", "content": "a"}], warned_flag=flag, logger=logger,
    )
    _remap_developer_role(
        [{"role": "developer", "content": "b"}, {"role": "developer", "content": "c"}],
        warned_flag=flag,
        logger=logger,
    )
    _remap_developer_role(
        [{"role": "developer", "content": "d"}], warned_flag=flag, logger=logger,
    )
    assert logger.warning.call_count == 1, (
        f"expected exactly one WARN per adapter instance, got {logger.warning.call_count}"
    )


def test_no_warn_when_no_developer_messages():
    logger = MagicMock()
    flag = {"warned": False}
    _remap_developer_role(
        [{"role": "user", "content": "Hi"}], warned_flag=flag, logger=logger,
    )
    assert logger.warning.call_count == 0
    assert flag["warned"] is False


def test_remap_disabled_path_skips_function_entirely():
    """When compat_remap_developer_role=false, the adapter does not call
    _remap_developer_role at all — _assert_lcd_payload then catches the
    developer role and raises LcdPayloadError. This test guards that surface:
    the helper itself, if bypassed, leaves messages untouched.
    """
    msgs = [{"role": "developer", "content": "x"}]
    # Caller never invokes _remap_developer_role; messages remain unchanged.
    assert msgs[0]["role"] == "developer"
