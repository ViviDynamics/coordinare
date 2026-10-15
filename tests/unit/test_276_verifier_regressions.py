"""Executable regressions for diagnostic framing and verification verdicts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.cache_provenance import RESULT_SENTINEL

from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.activity_log import ActivityLog
from coordinare.services.env_bootstrap_control import reset_env_bootstrap_breaker
from coordinare.services.env_cache import _INFRA_FAILURE_CAP, EnvCacheService
from coordinare.services.env_manifest import _service_readiness_check, render_verify_sh
from coordinare.services.env_provenance import DEFAULT_LIMITS
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_verifier import WALK_DIAGNOSTIC_PREFIX
from tests.unit.services.test_400_forced_regen_single_bootstrap import _cycle, _github
from tests.unit.services.test_env_cache_phase4 import _make_cfg, _seed_state
from tests.unit.test_265_k8s_verifier import _manifest, _run_in_pod
from tests.unit.test_265_provenance_k8s import ACTIVATE, freeze, logs, setup, summary
from tests.unit.test_276_review_blockers import (
    SHELL_EXECUTABLE_MODE,
    WALK_EXIT_CODE,
    _walker_output,
)

SHELL_TIMEOUT_SECONDS = 10
PIPE_BUFFER_EXCEEDING_LINES = 100_000
MYSQL_PORT = 3306
VERDICT_FAILURE = 1
SHA256_HEX_CHARS = 64
UPDATED_STAT_DIGEST = "d" * SHA256_HEX_CHARS
REDACTED = "[REDACTED]"


def _run_shell(tmp_path: Path, script: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "verify.sh"
    path.write_text(script)
    return subprocess.run(
        ["bash", str(path)], capture_output=True, text=True, check=False,
        env={**os.environ, "BASH_ENV": "", "ENV": "", **extra_env}, timeout=SHELL_TIMEOUT_SECONDS,
    )


def _run_verify(tmp_path: Path, script: str, execution: str) -> subprocess.CompletedProcess[str]:
    if execution == "standalone":
        return _run_shell(tmp_path, script)
    pod = _manifest(mount_path=str(tmp_path), verify_text=script, project_files={})
    return _run_in_pod(pod, tmp_path)


@pytest.mark.parametrize("execution", ["standalone", "kubernetes"])
@pytest.mark.parametrize("kind", ["runtime", "gem"])
@pytest.mark.parametrize("check", ["false", "false; echo OK: tool", "false | cat; echo OK: tool"])
def test_hard_check_failure_cannot_be_masked(
    tmp_path: Path, execution: str, kind: str, check: str,
) -> None:
    manifest = EnvManifest(symphony_name="sym", items=[
        ManifestItem(name="custom", kind=kind, source="README.md", check=check),
    ])
    result = _run_verify(tmp_path, render_verify_sh(manifest, cache_mount_path=str(tmp_path)), execution)
    assert result.returncode == VERDICT_FAILURE, result.stdout + result.stderr
    assert "verification FAILED" in result.stderr


def test_walk_diagnostic_cache_filename_cannot_imitate_result_sentinel(tmp_path: Path) -> None:
    filename = tmp_path / f"{RESULT_SENTINEL}cache{RESULT_SENTINEL}entry"
    diagnostic = f"PermissionError: cannot read {filename}"
    result = _walker_output(tmp_path, logs(summary()), diagnostic, 0)
    assert result.returncode == 0
    assert result.stdout.count(RESULT_SENTINEL) == 1
    assert result.stderr == ""
    assert "cannot read" in result.stdout
    assert parse_summary(result.stdout, ACTIVATE, DEFAULT_LIMITS) == summary()


def test_parser_ignores_sentinels_in_prefixed_walk_diagnostics() -> None:
    diagnostic = WALK_DIAGNOSTIC_PREFIX + json.dumps(f"PermissionError: {RESULT_SENTINEL}cache") + "\n"
    assert parse_summary(diagnostic + logs(summary()), ACTIVATE, DEFAULT_LIMITS) == summary()


def test_parser_still_rejects_sentinels_in_other_log_lines() -> None:
    with pytest.raises(ValueError, match="duplicated"):
        parse_summary(f"untrusted {RESULT_SENTINEL}cache\n" + logs(summary()), ACTIVATE, DEFAULT_LIMITS)


@pytest.mark.parametrize(("diagnostic", "secret", "expected"), [
    ("credential ghp_classicCredential", "ghp_classicCredential", REDACTED),
    ("credential github_pat_fineGrainedCredential", "github_pat_fineGrainedCredential", REDACTED),
    ("credential sk-vendorCredential", "sk-vendorCredential", REDACTED),
    ("credential AKIAcloudCredential", "AKIAcloudCredential", REDACTED),
    ("Authorization: bEaReR bearerCredential", "bearerCredential", "Bearer " + REDACTED),
    ("https://alice:urlCredential@example.org/cache", "alice:urlCredential", "https://" + REDACTED + "@example.org"),
    ('API_KEY="assignment credential"', "assignment credential", "API_KEY=" + REDACTED),
])
def test_each_walk_diagnostic_redaction_pattern(
    tmp_path: Path, diagnostic: str, secret: str, expected: str,
) -> None:
    result = _walker_output(tmp_path, logs(summary()), diagnostic, 0)
    prefix = result.stdout.removesuffix(logs(summary()))
    assert result.returncode == 0
    assert secret not in prefix
    assert expected in prefix
    assert parse_summary(result.stdout, ACTIVATE, DEFAULT_LIMITS) == summary()


@pytest.mark.parametrize("activation", ["grep -q zzzz /etc/hosts\n", "false\n"])
@pytest.mark.parametrize("execution", ["standalone", "kubernetes"])
def test_benign_activation_failure_does_not_fail_verify(tmp_path: Path, activation: str, execution: str) -> None:
    manifest = EnvManifest(symphony_name="sym", items=[
        ManifestItem(name="required", kind="runtime", source="README.md", check="echo 'OK: required'"),
        ManifestItem(name="coordinare_missing_optional_tool", kind="system", source="README.md"),
    ])
    verify = render_verify_sh(manifest, cache_mount_path=str(tmp_path), activate_text=activation)
    result = _run_verify(tmp_path, verify, execution)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN:" in result.stdout
    assert "PASSED" in result.stdout


@pytest.mark.parametrize(("producer", "expected"), [("false", VERDICT_FAILURE), ("printf ok", 0)])
def test_rendered_verify_enforces_pipefail_without_wrapper(tmp_path: Path, producer: str, expected: int) -> None:
    check = f"if {producer} | cat; then echo OK; else FAILED=1; fi"
    manifest = EnvManifest(symphony_name="sym", items=[
        ManifestItem(name="custom", kind="runtime", source="README.md", check=check),
    ])
    result = _run_shell(tmp_path, render_verify_sh(manifest, cache_mount_path=str(tmp_path)))
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("runtime", ["ruby", "node"])
@pytest.mark.parametrize("exit_code", [0, WALK_EXIT_CODE])
def test_version_check_preserves_failed_producer_without_err_trap(tmp_path: Path, runtime: str, exit_code: int) -> None:
    version = "3.4.2" if runtime == "ruby" else "20.11.0"
    binary = tmp_path / runtime
    binary.write_text(f"#!/bin/bash\nprintf '{version}\\n'\nexit {exit_code}\n")
    binary.chmod(SHELL_EXECUTABLE_MODE)
    manifest = EnvManifest(symphony_name="sym", items=[
        ManifestItem(name=runtime, kind="runtime", version=version, source="README.md"),
    ])
    verify = render_verify_sh(manifest, cache_mount_path=str(tmp_path))
    result = _run_shell(tmp_path, verify, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert result.returncode == (0 if exit_code == 0 else VERDICT_FAILURE), result.stdout + result.stderr


@pytest.mark.parametrize("execution", ["standalone", "kubernetes"])
@pytest.mark.parametrize("kind", ["system", "node_pkg"])
@pytest.mark.parametrize("check", ["false", "false\necho 'WARN: optional'"])
def test_benign_failure_in_custom_soft_check_does_not_fail_verify(
    tmp_path: Path, execution: str, kind: str, check: str,
) -> None:
    manifest = EnvManifest(symphony_name="sym", items=[
        ManifestItem(name="required", kind="runtime", source="README.md", check="echo 'OK: required'"),
        ManifestItem(name="optional", kind=kind, source="README.md", check=check),
    ])
    result = _run_verify(tmp_path, render_verify_sh(manifest, cache_mount_path=str(tmp_path)), execution)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN:" in result.stdout
    assert "verification PASSED" in result.stdout


@pytest.mark.parametrize("exit_code", [0, WALK_EXIT_CODE])
def test_mysql_ping_consumes_large_output_and_preserves_producer_status(tmp_path: Path, exit_code: int) -> None:
    binary = tmp_path / "mariadb-admin"
    binary.write_text(
        "#!/bin/bash\nprintf 'mysqld is alive\\n'\n"
        f"for ((line=0; line<{PIPE_BUFFER_EXCEEDING_LINES}; line++)); do printf 'extra diagnostic output\\n'; done\n"
        f"exit {exit_code}\n",
    )
    binary.chmod(SHELL_EXECUTABLE_MODE)
    service = SimpleNamespace(name="mysql", kind="mysql", port=MYSQL_PORT)
    check = _service_readiness_check(service)
    assert check is not None
    script = f'set -o pipefail\nFAILED=0\n{check}\nexit "$FAILED"\n'
    result = _run_shell(tmp_path, script, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert result.returncode == (0 if exit_code == 0 else VERDICT_FAILURE), result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaustion", ["attempts", "infrastructure", "configuration"])
@pytest.mark.parametrize("restart", [False, True])
async def test_only_configuration_exhaustion_disables_consumer_recheck(
    tmp_path: Path, exhaustion: str, restart: bool,
) -> None:
    result = summary(stat_digest=UPDATED_STAT_DIGEST)
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [
        (True, logs(summary())), (True, logs(result)),
    ])
    await freeze(service, state, daemon, performer)
    if exhaustion == "configuration":
        service.refuse_verifier_configuration(ec, state, "invalid image")
    else:
        failure_budget = (_INFRA_FAILURE_CAP if exhaustion == "infrastructure"
                          else state["coordinare_config"].global_config.env_bootstrap_max_attempts)
        for _attempt in range(failure_budget):
            service.on_bootstrap_complete("sym", False, state, error="failure", infra_failure=exhaustion == "infrastructure")
    assert ec.bootstrap_exhausted
    if restart:
        snapshot = _persist_env_cache(state["env_cache"])["sym"]
        ec = EnvCacheState(symphony_name="sym", sanitised_name=ec.sanitised_name, cache_dir=ec.cache_dir)
        _rehydrate_env_cache({"env_cache": {"sym": ec}}, SimpleNamespace(env_cache={"sym": snapshot}))
        state["env_cache"]["sym"] = ec
    calls_before = len(runtime.specs)
    attempts_before = ec.bootstrap_attempts
    await service.recheck_consumer_provenance("sym", state)
    if exhaustion == "configuration":
        assert len(runtime.specs) == calls_before
        assert ec.bootstrap_exhaustion_reason == "configuration_refusal"
    else:
        assert len(runtime.specs) == calls_before + 1
        assert ec.provenance_stat_digest == UPDATED_STAT_DIGEST
        assert ec.consumer_stat_refusals == 1
        assert ec.bootstrap_exhaustion_reason is None
    assert ec.bootstrap_attempts == attempts_before
    assert ec.bootstrap_exhausted
    assert not ec.runtime_health_failed


@pytest.mark.parametrize("reset", ["manual", "success"])
def test_configuration_exhaustion_reason_clears_when_breaker_rearms(tmp_path: Path, reset: str) -> None:
    service, ec, state, _daemon, _performer, _runtime = setup(tmp_path, [])
    service.refuse_verifier_configuration(ec, state, "invalid image")
    assert ec.bootstrap_exhaustion_reason == "configuration_refusal"
    if reset == "manual":
        reset_env_bootstrap_breaker(state, "sym", ec, "operator", ActivityLog())
    else:
        service.on_bootstrap_complete("sym", True, state)
    assert not ec.bootstrap_exhausted
    assert ec.bootstrap_exhaustion_reason is None


@pytest.mark.asyncio
async def test_spec_change_clears_configuration_exhaustion_reason(tmp_path: Path) -> None:
    name = "my-project"
    state, _cache, ec = _seed_state(tmp_path, name)
    cfg, sym = _make_cfg(name, tmp_path)
    service = EnvCacheService(cfg)
    github = _github("old-sha")
    ec.readme_sha = await service._current_manifest_sha(name, sym, github, sym.effective_config(cfg.global_config), ec)
    service.refuse_verifier_configuration(ec, state, "invalid image")
    assert ec.bootstrap_exhaustion_reason == "configuration_refusal"
    await _cycle(service, name, sym, _github("new-sha"), state, [])
    assert not ec.bootstrap_exhausted
    assert ec.bootstrap_exhaustion_reason is None
