"""A provenance refusal after a passed clean execution is a repairable failure, not lost cleanup.

Dettmore e2e-92 passed the installer and every clean check, then the provenance freeze refused a link out of the
cache. With no failed result to admit, the attempt held as cleanup_unconfirmed although its cleanup was confirmed.
"""
from __future__ import annotations

import json

from coordinare_service_inference.environment.models import EnvironmentManifest, VerificationReport

from coordinare.services.environment_repair import (
    provenance_refusal_evidence,
    record_execution_failure,
)
from coordinare.services.environment_repair_triage import PROVENANCE, triage_failure
from tests.unit.env_manifest_fixture import reviewed_metadata, verification_output
from tests.unit.test_353_execution_repair import cache_fixture, started

REASON = "env-cache provenance refused: link deps/usr/lib/libcrypto.so points outside the cache"


def _passed(cache):
    cache.environment_verification = json.loads(verification_output(cache.environment_manifest))
    return cache


def test_the_refusal_becomes_a_failed_provenance_result_that_requests_a_repair(tmp_path):
    cache = _passed(cache_fixture(tmp_path))
    started(cache)
    evidence = provenance_refusal_evidence(cache, REASON)
    assert evidence is not None and evidence["results"][-1]["id"] == PROVENANCE
    assert record_execution_failure(cache, evidence, cleanup_confirmed=True, now=101)
    assert cache.environment_attempt["status"] == "repair_pending"
    rejected = cache.environment_attempt["rejected"][-1]["report"]["results"][-1]
    assert rejected["id"] == PROVENANCE and REASON in rejected["output"]


def test_its_repair_may_change_every_install():
    data = reviewed_metadata()["environment_manifest"]
    data["install"] = [{"id": "install-deps", "argv": ["true", "deps"]}, {"id": "install-tools", "argv": ["true", "tools"]}]
    manifest = EnvironmentManifest.model_validate(data)
    data = json.loads(verification_output(manifest.model_dump(mode="json")))
    data.pop("passed", None)
    report = VerificationReport.model_validate(data)
    report.results.append(report.results[0].model_copy(update={"id": PROVENANCE, "status": "fail", "exit_code": None,
                                                                "output": REASON}))
    triage = triage_failure(manifest, report)
    assert triage.roots == {PROVENANCE: "provenance"}
    assert {"install-deps", "install-tools"} <= set(triage.closure_ids)


def test_any_other_text_is_not_admitted_as_a_provenance_refusal(tmp_path):
    cache = _passed(cache_fixture(tmp_path))
    assert provenance_refusal_evidence(cache, "no daemon-rendered activate.sh on record") is None
