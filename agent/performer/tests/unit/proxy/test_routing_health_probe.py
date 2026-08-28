"""099 (T002): TargetDescriptor.health_probe selector + fail-fast validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.proxy.routing import TargetDescriptor


def _kwargs(**over):
    base = dict(
        base_url="http://192.0.2.10:11434",
        wire_format="openai",
        strategy="normalize",
        normalizers=["strip_control_chars", "strip_reasoning"],
    )
    base.update(over)
    return base


def test_health_probe_defaults_to_tool_call() -> None:
    t = TargetDescriptor(**_kwargs())
    assert t.health_probe == "tool_call"


def test_health_probe_completion_accepted() -> None:
    t = TargetDescriptor(**_kwargs(health_probe="completion"))
    assert t.health_probe == "completion"


def test_health_probe_invalid_value_fails_fast() -> None:
    with pytest.raises(ValidationError):
        TargetDescriptor(**_kwargs(health_probe="bogus"))
