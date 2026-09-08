"""Named real-source rule mutations; always restore and verify the original hash."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = "agent/performer/src/performer/workflows/env_bootstrap.py"
ENV = "agent/performer/src/performer/workflows/bootstrap_environment.py"
TK = "agent/performer/src/performer/workflows/toolkit.py"
ADAPTER = "agent/performer/src/performer/workflows/adapter.py"
WS = "agent/performer/src/performer/workspace.py"
DAEMON = "src/coordinare/daemon.py"
MAIN = "agent/performer/src/performer/main.py"
TEST = "tests/unit/workflows/test_env_bootstrap.py"
DISPATCH = "tests/unit/test_174_bootstrap_dispatch.py"
TERMINAL = "agent/performer/tests/unit/test_main.py"

# Each tuple changes one production rule, never the test's expected outcome.
CASES = [
    ("repair_failure_feedback", WF, '+ feedback if attempt else', '+ "ignored" if attempt else', TEST, 'test_install_repair_and_integrity'),
    ("artifact_identity", ENV, 'self._fingerprint(name) == expected', 'True', TEST, 'test_install_repair_and_integrity'),
    ("promised_verifier", ENV, 'if promised:', 'if False:', TEST, 'test_missing_promised_verifier_fails_before_install'),
    ("readiness_order", WF, 'checked = await step("readiness")', 'checked = await step("verify")', TEST, 'test_readiness_precedes_verification_and_respects_mode'),
    ("service_mode", ENV, 'if not self.score.coordinare_manages_services:', 'if True:', TEST, 'test_readiness_precedes_verification_and_respects_mode'),
    ("zero_repairs", WF, 'value = int(env.get(key, str(default)))', 'value = max(1, int(env.get(key, str(default))))', TEST, 'test_zero_repair_budget'),
    ("budget_range", WF, 'if not low <= value <= high:', 'if False:', TEST, 'test_invalid_budget_fails_before_install'),
    ("missing_verifier", ENV, '"passed": passed is True', '"passed": passed is not False', TEST, 'test_missing_unpromised_verifier_is_still_not_success'),
    ("symlink_artifact", ENV, 'not stat.S_ISREG(info.st_mode) or ', '', TEST, 'test_symlinked_promised_artifact_fails_before_install'),
    ("inference_timeout", ENV, 'except TimeoutError:', 'except FileNotFoundError:', TEST, 'test_inference_timeout_remains_best_effort'),
    ("readiness_result", ENV, '"passed": passed, "reason":', '"passed": True, "reason":', TEST, 'test_readiness_failure_is_repaired_before_verify'),
    ("step_metrics", WF, 'durations[name] = durations.get(name, 0) + int((time.monotonic() - started) * 1000)', 'pass', TEST, 'test_adapter_reports_named_steps_and_real_metrics'),
    ("cancel_accounting", TK, 'except asyncio.CancelledError:\n            wall_ms = int((time.monotonic() - started) * 1000)\n            self.metrics.agent_turns += 1', 'except asyncio.CancelledError:\n            wall_ms = int((time.monotonic() - started) * 1000)\n            self.metrics.agent_turns += 0', TEST, 'test_timeout_stops_harness_without_repair'),
    ("cancel_propagation", TK, '            raise\n        except BaseException as exc:', '            return {"exit_state": "error"}\n        except BaseException as exc:', TEST, 'test_cancellation_stops_harness_and_propagates'),
    ("start_cancel_cleanup", ADAPTER, 'except asyncio.CancelledError:\n            await backend.stop()\n            raise\n        except Exception as exc:', 'except asyncio.CancelledError:\n            raise\n        except Exception as exc:', TEST, 'test_cancellation_during_harness_start_stops_it'),
    ("harness_failure", WF, 'if turn["exit_state"] != "done":', 'if False:', TEST, 'test_harness_error_never_runs_verification_or_repair'),
    ("cache_path", ENV, 'if not self.score.env_cache_path or not Path(self.score.env_cache_path).is_absolute():', 'if False:', TEST, 'test_no_cache_fails_before_install'),
    ("verify_cancel_cleanup", WS, '            await _kill_cache_process_group(proc)\n    out = out_b.decode', '            pass\n    out = out_b.decode', TEST, 'test_cancelled_verification_reaps_subprocess'),
    ("service_start_cancel_cleanup", WS, 'except asyncio.CancelledError:\n            if proc is not None:\n                await _kill_cache_process_group(proc)\n            raise', 'except asyncio.CancelledError:\n            raise', TEST, 'test_cancelled_service_gate_cannot_write_after_timeout'),
    ("service_health_cancel_cleanup", WS, 'except asyncio.CancelledError:\n        if proc is not None:\n            await _kill_cache_process_group(proc)\n        raise', 'except asyncio.CancelledError:\n        raise', TEST, 'test_cancelled_service_gate_cannot_write_after_timeout'),
    ("service_children", WS, "os.killpg(proc.pid, signal.SIGKILL)", "proc.kill()", TEST, "test_cancelled_service_gate_cannot_write_after_timeout"),
    ("workflow_dispatch", DAEMON, 'dispatch_dict["workflow"] = workflow', 'dispatch_dict["workflow"] = "noop"', DISPATCH, 'test_bootstrap_only_uses_its_own_workflow_and_round_trips_flags'),
    ("symphony_override", DAEMON, 'cfg = symphony_cfg.effective_config(coordinare_cfg.global_config)', 'cfg = self._state.get("config")', DISPATCH, 'test_symphony_override_selects_bootstrap_workflow'),
    ("post_verify_integrity", ENV, 'self.score.env_cache_path, self.stand.cache_env,\n            )\n            integrity = await self.run("integrity")', 'self.score.env_cache_path, self.stand.cache_env,\n            )\n            integrity = {"passed": True}', TEST, 'test_verifier_cannot_rewrite_activation_and_report_success'),
    ("terminal_inference_fields", MAIN, 'if any(not key.startswith("inference_")', 'if False and any(not key.startswith("inference_")', TERMINAL, 'test_bootstrap_workflow_terminal_skips_legacy_tail'),
    ("invalid_dispatch_timeout", DAEMON, "except ValueError:\n                    pass  # Preserve", "except TypeError:\n                    pass  # Preserve", DISPATCH, "test_bootstrap_only_uses_its_own_workflow_and_round_trips_flags"),
]


def main() -> None:
    env = dict(os.environ, PYTHONPATH="src:agent/performer/src", PYTHONDONTWRITEBYTECODE="1")
    failures = []
    cases = CASES
    if len(sys.argv) > 1:
        start = next(i for i, case in enumerate(CASES) if case[0] == sys.argv[1])
        cases = CASES[start:]
    for name, filename, old, new, test, selector in cases:
        path = ROOT / filename
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        source = original.decode()
        if source.count(old) != 1:
            raise RuntimeError(f"{name}: mutation anchor matched {source.count(old)} times")
        try:
            path.write_text(source.replace(old, new, 1))
            run = subprocess.run([sys.executable, "-B", "-m", "pytest", test, "-k", selector,
                                  "-q", "--timeout=8"], cwd=ROOT, env=env,
                                 text=True, capture_output=True, timeout=25)
            killed = run.returncode == 1 and "failed" in run.stdout and "ERROR collecting" not in run.stdout
            if not killed:
                failures.append(name)
                print(run.stdout[-2000:] + run.stderr[-1000:], flush=True)
        finally:
            path.write_bytes(original)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"{name}: restore hash mismatch")
        print(f"{name}: {'KILLED' if killed else 'MISSED/INVALID'}; restored sha256={digest}", flush=True)
    if failures:
        raise SystemExit(f"Undetected or invalid mutations: {failures}")


if __name__ == "__main__":
    main()
