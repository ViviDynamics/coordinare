"""Issue #228: the daemon validates dashboard routing writes without the performer installed.

The production daemon image runs ``uv sync`` for the ``coordinare`` project only, so it has no
``performer`` package. Routing validation used to import ``performer.proxy.routing`` lazily, and
every save was refused with "Routing validation is unavailable". The models now come from the
shared ``coordinare-routing-models`` package.

The check runs in a fresh interpreter that has only ``src`` and the shared package on its path,
with a meta-path finder that refuses every ``performer`` import. That reproduces the image without
depending on what the test process happens to have installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COORDINARE_SRC = REPO_ROOT / "src"
SHARED_SRC = REPO_ROOT / "packages" / "routing_models" / "src"
SUBPROCESS_TIMEOUT_SECONDS = 120

# Run with ``-I`` (no PYTHONPATH, no user site), with the two source roots passed as argv.
# Installed third-party deps (pydantic, pyyaml, structlog) still come from the venv; only the
# ``performer`` package is made unreachable.
_DAEMON_PROGRAM = """
import importlib.abc
import json
import sys

class _NoPerformer(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "performer" or name.startswith("performer."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, _NoPerformer())
sys.path[:0] = [sys.argv[1], sys.argv[2]]

from coordinare import routing_config_service as rcs

valid = {
    "backend": "vllm",
    "model": "qwen2.5-coder",
    "target": {
        "base_url": "http://localhost:8000/v1",
        "wire_format": "openai",
        "strategy": "normalize",
        "normalizers": ["harmony_tool_calls"],
    },
}
invalid = {
    "backend": "vllm",
    "model": "qwen2.5-coder",
    "target": {
        "base_url": "http://localhost:8000/v1",
        "wire_format": "openai",
        "strategy": "normalize",
        "normalizers": ["no_such_normalizer"],
    },
}

result = {"performer_imported": "performer" in sys.modules}
try:
    rcs.validate_routing_entry(valid)
    result["valid"] = "ok"
except ValueError as exc:
    result["valid"] = f"error: {exc}"
try:
    rcs.validate_routing_entry(invalid)
    result["invalid"] = "ok"
except ValueError as exc:
    result["invalid"] = f"error: {exc}"
try:
    rcs._validate_table([valid])
    result["table"] = "ok"
except ValueError as exc:
    result["table"] = f"error: {exc}"
print(json.dumps(result))
"""


def _run_daemon_probe() -> dict[str, object]:
    proc = subprocess.run(  # fixed argv, no shell
        [sys.executable, "-I", "-c", _DAEMON_PROGRAM, str(COORDINARE_SRC), str(SHARED_SRC)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_daemon_environment_validates_routing_without_performer() -> None:
    result = _run_daemon_probe()
    assert result["performer_imported"] is False
    assert result["valid"] == "ok"
    assert result["table"] == "ok"


def test_daemon_environment_rejects_invalid_routing_entry_with_model_message() -> None:
    result = _run_daemon_probe()
    invalid = str(result["invalid"])
    assert invalid.startswith("error: ")
    # The real pydantic message, not the old "unavailable" refusal.
    assert "unavailable" not in invalid
    assert "no_such_normalizer" in invalid
