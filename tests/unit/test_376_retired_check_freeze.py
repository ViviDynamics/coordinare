"""nare-native-023: a delta repair after a retirement was refused whatever the author returned.

Repair 2 retired two checks (required false). Repair 4's author kept them byte for byte, yet the closure guard named
them as changed fifteen times: preservation viewed every retired check as required, including checks the previous
generation had already retired, so the view never matched it.
"""
from __future__ import annotations

from coordinare.services.environment_obligations import guard_repair_diff, preservation_view
from tests.unit.test_353_repair_freeze import build, cmd, expect_rejected, triage_of


def previous_with_a_retired_check():
    return build(
        install=[cmd("install-base"), cmd("install-tool", ["make", "tool"], depends_on=["install-base"]),
                 cmd("install-other", ["make", "other"])],
        checks=[cmd("check-tool", ["tool"]), cmd("check-other", ["other"]),
                cmd("check-old", ["old"], required=False), cmd("check-new", ["new"])],
        criterion_checks={"S0123456789ABCDEF": ["check-other", "check-new"]},
    )


def test_a_check_retired_by_an_earlier_generation_is_judged_as_the_previous_one_had_it():
    previous = previous_with_a_retired_check()
    triage = triage_of(previous)
    install = [item.model_copy(update={"argv": ["make", "tool", "PREFIX=/opt"]}) if item.id == "install-tool" else item
               for item in previous.install]
    candidate = previous.model_copy(update={"install": install})

    view = preservation_view(candidate, previous, {"check-old"})
    assert next(check for check in view.checks if check.id == "check-old").required is False
    guard_repair_diff(view, previous, triage)


def test_a_check_retired_now_is_still_judged_as_the_required_check_it_was():
    previous = previous_with_a_retired_check()
    triage = triage_of(previous)
    checks = [item.model_copy(update={"required": False}) if item.id == "check-tool" else item for item in previous.checks]
    candidate = previous.model_copy(update={"checks": checks})

    view = preservation_view(candidate, previous, {"check-old", "check-tool"})
    assert next(check for check in view.checks if check.id == "check-tool").required is True
    guard_repair_diff(view, previous, triage)
    # Weakening it in place, rather than through a retirement, is still a frozen change.
    expect_rejected(candidate, previous, triage, "check-tool")
