"""A correction that drops an obligation is told what to restore, not held (in repairs and first discovery alike).

Dettmore e2e-84: the trial passed every service command, then the repair author's fourth reply dropped a required
obligation. During a repair obligation_removed was fatal, so the attempt held with three repairs unused, and its
journal recorded no message saying what was lost.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import OBLIGATION_RESTORE, _declaration_feedback


def test_a_removed_obligation_is_correctable_feedback_naming_what_to_restore():
    message = "required source criterion coverage removed during repair: criterion_checks for S1 must still include c1"
    feedback = _declaration_feedback(EnvironmentDiscoveryError(message, code="obligation_removed"),
                                     SimpleNamespace(), SimpleNamespace(retirable_checks=frozenset(), execution_repairs=1))
    assert feedback == [{"location": [], "code": "obligation_removed", "guidance": message + OBLIGATION_RESTORE}]


def test_an_uncorrectable_refusal_still_raises():
    with pytest.raises(EnvironmentDiscoveryError):
        _declaration_feedback(EnvironmentDiscoveryError("x", code="attempt_budget_identity"),
                              SimpleNamespace(), SimpleNamespace(retirable_checks=frozenset()))


