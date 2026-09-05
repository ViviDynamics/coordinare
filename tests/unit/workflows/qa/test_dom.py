"""The DOM reader must wait for client-side rendering (round-two review, critical).

A real browser cannot run in the unit suite, so this pins the contract by
source inspection: a future edit that drops the wait reintroduces the false
regression on every SPA.
"""
from __future__ import annotations

import inspect

from performer.workflows.qa import dom


def test_the_reader_waits_for_the_network_to_settle_not_just_the_load_event():
    src = inspect.getsource(dom.read_dom)
    assert 'wait_until="networkidle"' in src, (
        "the default waitUntil='load' returns before client-side JS renders; a "
        "SPA then reads as an empty page at baseline -- a false regression"
    )


def test_the_reader_still_fails_loudly_on_an_unreadable_page():
    """Unchanged guarantee: an unreadable page must not read as an empty one."""
    src = inspect.getsource(dom.read_dom)
    assert "raise RuntimeError" in src
    assert "return []" not in src.split("except")[1], "no silent empty list on failure"
