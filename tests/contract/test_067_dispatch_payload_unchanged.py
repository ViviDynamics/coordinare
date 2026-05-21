"""Guard test (spec 067 T009.5): no new fields cross coordinare → performer.

The compat_remap_developer_role flag is performer-local (read by the adapter
from its loaded config.yaml). Per plan.md "Dispatch payload" note, 067
adds no entry to ``specs/contracts/dispatch-payload.md``. This test fails
loudly if a future edit accidentally leaks a 067-flavoured field across the
boundary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_CONTRACT = REPO_ROOT / "specs" / "contracts" / "dispatch-payload.md"

# Fields that MUST stay performer-local — never appear in dispatch payload.
_PERFORMER_LOCAL_FIELDS: tuple[str, ...] = (
    "compat_remap_developer_role",
)

pytestmark = pytest.mark.contract


def test_dispatch_contract_has_no_compat_fields():
    text = DISPATCH_CONTRACT.read_text()
    for field in _PERFORMER_LOCAL_FIELDS:
        assert field not in text, (
            f"Dispatch payload contract leaked performer-local field {field!r}; "
            "spec 067 specifies this flag is read by the adapter from config.yaml, "
            "not dispatched per-card."
        )


def test_agent_service_does_not_forward_compat_fields():
    """agent_service.py is the coordinare-side dispatch builder. None of the
    067 performer-local flags should appear in its source.
    """
    agent_service = REPO_ROOT / "src" / "coordinare" / "services" / "agent_service.py"
    if not agent_service.exists():
        pytest.skip("agent_service.py not present in this checkout")
    text = agent_service.read_text()
    for field in _PERFORMER_LOCAL_FIELDS:
        assert field not in text, (
            f"agent_service.py references {field!r}; this flag is performer-local "
            "and must not cross the dispatch boundary (spec 067)."
        )
