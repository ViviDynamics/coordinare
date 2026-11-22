"""nare-native-028: the global delta author reordered the activation PATH entries, outside its failure closure. The
guard refused it nine times, naming only "activation", and the delta was rejected; the full-repair fallback then cost
the attempt its remaining time. A frozen record may only be restored, so the daemon restores it, as it already does
for frozen install and check records, and keeps the growth the guard allows."""
from __future__ import annotations

import copy

import pytest

from coordinare.services.environment_delta import DeltaPlan
from coordinare.services.environment_repair_triage import ACTIVATION, triage_failure
from tests.unit.test_353_delta_repair import env, load_gen0


def _plan() -> tuple[DeltaPlan, dict]:
    base = load_gen0()[0]
    report = env.VerificationReport(manifest_digest=base.digest(), results=[
        env.CheckResult(id="check-ruby-pinned", status="fail", required=True, exit_code=1, duration_seconds=1,
                        output="assertion failed")])
    plan = DeltaPlan(base=base, triage=triage_failure(base, report))
    assert ACTIVATION not in plan.closure
    return plan, plan.base_proposal()


def test_reordered_frozen_activation_is_restored_keeping_added_entries() -> None:
    plan, base = _plan()
    candidate = copy.deepcopy(base)
    old_path, old_env = list(base["activation"]["path"]), dict(base["activation"]["env"])
    candidate["activation"] = {"path": [*reversed(old_path), "{cache}/extra/bin"],
                               "env": {**dict.fromkeys(old_env, "changed"), "EXTRA": "1"}}

    with pytest.raises(Exception) as refused:
        plan.guard_part(base, candidate)
    assert getattr(refused.value, "code", None) == "repair_outside_closure"

    restored = plan.restore_frozen(base, candidate)
    assert restored["activation"]["path"] == [*old_path, "{cache}/extra/bin"]
    assert restored["activation"]["env"] == {**old_env, "EXTRA": "1"}
    plan.guard_part(base, restored)


def test_dropped_frozen_activation_is_restored() -> None:
    plan, base = _plan()
    candidate = {key: value for key, value in copy.deepcopy(base).items() if key != "activation"}
    # The failed check itself changes, as a repair must (nare-native-029); only activation is under test here.
    candidate["checks"] = [{**check, "timeout_seconds": 900} if check["id"] == "check-ruby-pinned" else check
                           for check in candidate["checks"]]

    restored = plan.restore_frozen(base, candidate)
    assert restored["activation"] == {"path": base["activation"]["path"], "env": base["activation"]["env"]}
    plan.guard_part(base, restored)


def test_unchanged_activation_is_left_alone() -> None:
    plan, base = _plan()
    candidate = copy.deepcopy(base)
    assert plan.restore_frozen(base, candidate)["activation"] == base["activation"]
