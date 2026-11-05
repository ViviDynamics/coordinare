"""Measure SC-006 contract processing; excludes subprocess, model and provenance work.

Run with the checkout's interpreter: .venv/bin/python scripts/benchmark_environment_manifest.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
from coordinare_service_inference.environment.models import (
    MAX_DOCUMENT_BYTES,
    CoverageAssessment,
    EnvironmentManifest,
    ExecutionCapabilities,
    VerificationReport,
)
from coordinare_service_inference.environment.render import render_verify
from coordinare_service_inference.environment.scope import (
    ScopeAssessmentProposal,
    ScopeContract,
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    SourceAssessmentRecord,
    criterion_id,
)

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_acceptance import execution_report


def capabilities() -> ExecutionCapabilities:
    return ExecutionCapabilities(schema_version=1, image_ref='benchmark/image@sha256:'+'c'*64,
        platform=platform.system().lower()+'/'+platform.machine().lower(), cache_mount_path='/devenv/benchmark',
        install_network='enabled', verify_network='disabled')


def fixture(count: int = 1000) -> tuple[dict, dict, dict]:
    """Compact records still include every requirement and its own required check."""
    data = {
        "inventory_digest": "d" * 64, "capabilities_digest": capabilities().digest(), "schema_version": 3, "symphony_name": "benchmark", "source_commit": "a" * 40,
        "inputs": {"x": hashlib.sha256(b"benchmark requirements").hexdigest()},
        "requirements": [{"id": f"r{i}", "description": "x", "source": "x", "checks": [f"c{i}"]}
                         for i in range(count)],
        "checks": [{"id": f"c{i}", "argv": ["true"]} for i in range(count)],
        "install": [], "activation": {}, "services": [], "empty_reason": None, "policy_version": "353-scope-v1",
    }
    criterion = ScopeCriterionProposal(description='Synthetic benchmark obligations', sources=['x'],
        applicability='required', rationale='Synthetic performance fixture, never live project acceptance',
        usability='Every independently listed required check succeeds', runtime_needs=[])
    definition = ScopeDefinition(source_commit=data['source_commit'], inventory_digest=data['inventory_digest'],
        inputs=data['inputs'], criteria=[ScopeCriterion(id=criterion_id(criterion), **criterion.model_dump())])
    accepted = ScopeAssessmentProposal(verdict='accept', rationale='Synthetic benchmark source review',
        sources=['x'], findings=[], decisions=[])
    scope = ScopeContract(definition=definition, review_inputs=data['inputs'], sources=['x'], finding_responses=[],
        finding_ledger=FindingLedger(source_commit=data['source_commit'], reviews=[FindingReview(sequence=1,
            manifest_digest=definition.digest(), inputs=data['inputs'], decisions=[], added_ids=[])]),
        source_assessment=SourceAssessmentRecord(review_sequence=1, scope_digest=definition.digest(),
            source_commit=data['source_commit'], inventory_digest=data['inventory_digest'],
            reviewer_inputs=data['inputs'], assessment=accepted))
    data.update(scope_contract=scope.model_dump(mode='json'), scope_contract_digest=scope.digest(),
        criterion_checks={definition.criteria[0].id: [check['id'] for check in data['checks']]})
    manifest = EnvironmentManifest.model_validate(data)
    digest = manifest.digest()
    assessment = {
        "inventory_digest": "d" * 64, "capabilities_digest": manifest.capabilities_digest, "schema_version": 1, "symphony_name": "benchmark", "source_commit": manifest.source_commit,
        "manifest_digest": digest, "verdict": "accept", "rationale": "Benchmark fixture",
        "sources": ["x"], "inputs": data["inputs"], "policy_version": "351-v1",
    }
    source = ExecutionSourceIdentity(source_commit=manifest.source_commit,tree_digest="1"*64,file_count=1,expanded_bytes=22)
    report = {"execution_source_digest":source.digest(), "manifest_digest": digest, "platform": platform.system().lower() + "/" + platform.machine().lower(), "results": [
        {"id": f"c{i}", "status": "pass", "required": True, "exit_code": 0, "manifest_digest": digest}
        for i in range(count)
    ]}
    ledger = FindingLedger(source_commit=manifest.source_commit, reviews=[FindingReview(sequence=1,
        manifest_digest=manifest.digest(), inputs=dict(manifest.inputs), decisions=[], added_ids=[])])
    assessment.update(schema_version=2, policy_version='353-findings-v1',
        finding_ledger=ledger.model_dump(), finding_ledger_digest=ledger.digest())
    return manifest.model_dump(mode='json'), assessment, report


def process(data: dict, assessment: dict, report_data: dict) -> None:
    manifest = EnvironmentManifest.model_validate(data)
    digest = manifest.digest()  # Includes canonical encoding of every execution-affecting field.
    review = CoverageAssessment.model_validate(assessment)
    report = VerificationReport.model_validate(report_data)
    assert digest == review.manifest_digest == report.manifest_digest
    assert review.verdict == "accept" and report.passed
    by_id = {result.id: result for result in report.results}
    assert all(by_id[check.id].status == "pass" for check in manifest.checks if check.required)
    cache = EnvCacheState(
        symphony_name=manifest.symphony_name, sanitised_name="benchmark", cache_dir=Path("/benchmark"),
        bootstrap_commit_sha=manifest.source_commit, environment_manifest=data, environment_assessment=assessment,
        environment_capabilities=capabilities().model_dump(mode='json'),
        environment_execution_source=ExecutionSourceIdentity(source_commit=manifest.source_commit,tree_digest="1"*64,file_count=1,expanded_bytes=22).model_dump(),
    )
    assert execution_report(json.dumps(report_data), cache, publish=False).passed


def hardware() -> dict:
    result = {"platform": platform.platform(), "machine": platform.machine(),
              "python": platform.python_version(), "logical_cpus": os.cpu_count()}
    if platform.system() == "Darwin":
        for field, key in (("cpu", "machdep.cpu.brand_string"), ("memory_bytes", "hw.memsize")):
            result[field] = subprocess.check_output(["/usr/sbin/sysctl", "-n", key], text=True).strip()
    else:
        result["cpu"] = platform.processor()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--limit-ms", type=float, default=250.0)
    args = parser.parse_args()
    if args.iterations < 100 or args.warmup < 1 or args.limit_ms <= 0:
        parser.error("at least 100 samples, one warmup and a positive limit are required")
    data, assessment, report = fixture()
    manifest_bytes = len(EnvironmentManifest.model_validate(data).model_dump_json().encode())
    assert manifest_bytes <= MAX_DOCUMENT_BYTES
    for _ in range(args.warmup):
        process(data, assessment, report)
    samples = []
    for _ in range(args.iterations):
        start = time.perf_counter_ns()
        process(data, assessment, report)
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
    sys.stdout.write(json.dumps({"hardware": hardware(), "requirements": 1000, "checks": 1000,
                      "manifest_bytes": manifest_bytes, "document_cap_bytes": MAX_DOCUMENT_BYTES,
                      "assessment_bytes": len(CoverageAssessment.model_validate(assessment).model_dump_json().encode()),
                      "verification_bytes": len(VerificationReport.model_validate(report).model_dump_json().encode()),
                      "rendered_verify_bytes": len(render_verify(EnvironmentManifest.model_validate(data),
                                                                cache_mount_path="/devenv/benchmark").encode()),
                      "warmup": args.warmup, "iterations": args.iterations,
                      "median_ms": statistics.median(samples), "p95_ms": p95, "max_ms": max(samples),
                      "limit_ms": args.limit_ms, "passed": p95 <= args.limit_ms}, indent=2) + "\n")
    raise SystemExit(0 if p95 <= args.limit_ms else 1)


if __name__ == "__main__":
    main()
