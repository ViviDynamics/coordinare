"""#225: a routing table that already holds a duplicate (backend, model) pair.

New duplicates are refused. An older table can still hold them, so the Routing tab shows one
warning that names each pair and its positions. The page finds them in the server's own routing
read. The read keeps every duplicate, so the warning has the whole list. Unrelated writes still
land on the entry they name, and a new duplicate is still refused.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from coordinare.config_descriptors import SECRET_MASK
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.test_dashboard_config_api import _make_client, _routing_hash

_CONFIG_JS = _DASHBOARD_JS_SOURCES["config"]
_HELPERS_JS = _DASHBOARD_JS_SOURCES["helpers"]
_CHECKS = Path(__file__).resolve().parents[1] / "js" / "config_225_checks.js"


def _entry(backend: str, model: str) -> dict[str, Any]:
    return {"backend": backend, "model": model, "target": {
        "base_url": f"http://{backend}.local/v1", "wire_format": "openai", "strategy": "normalize",
        "normalizers": ["harmony_tool_calls"],
    }}


def _with_duplicates(routing_path: Path) -> None:
    """vllm/qwen at 0 and 2, ollama/llama at 1 and 4, and two entries that are unique."""
    entries = [
        _entry("vllm", "qwen2.5-coder"),
        _entry("ollama", "llama"),
        _entry("vllm", "qwen2.5-coder"),
        _entry("vllm", "coder-32b"),
        _entry("ollama", "llama"),
    ]
    routing_path.write_text(yaml.safe_dump({"entries": entries}, sort_keys=False))


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    return _make_client(config_path, raw)


def _fn(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def _var_line(source: str, name: str) -> str:
    match = re.search(rf"var {name} = [\s\S]*?;[ \t]*(?://[^\n]*)?\n", source)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def _sliced_js(tmp_path: Path) -> Path:
    """The shipped functions the Settings page runs for these checks, and their constants."""
    parts = [
        _fn(_HELPERS_JS, "esc"),
        _var_line(_CONFIG_JS, "CFG_SECRET_MASK"),
        _var_line(_CONFIG_JS, "CFG_INPUT"),
        _var_line(_CONFIG_JS, "CFG_HINT"),
        "var _CFG_REFS = {};",
        "var _cfgOptions = {endpoints: [], model_endpoints: [], modes: []};",
        _fn(_CONFIG_JS, "cfgRefSelect"),
        _fn(_CONFIG_JS, "cfgInput"),
        _fn(_CONFIG_JS, "cfgMayReveal"),
        _fn(_CONFIG_JS, "cfgDuplicatePairs"),
        _fn(_CONFIG_JS, "cfgDuplicateNotice"),
    ]
    path = tmp_path / "config_225.js"
    path.write_text("\n\n".join(parts), encoding="utf-8")
    return path


def _run_node(tmp_path: Path, served: Path | None = None) -> str:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    args = [node, str(_CHECKS), str(_sliced_js(tmp_path))]
    if served is not None:
        args.append(str(served))
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    return result.stdout


def test_the_server_read_keeps_every_duplicate(temp_config_with_routing: tuple[Path, Path]) -> None:
    config_path, routing_path = temp_config_with_routing
    _with_duplicates(routing_path)
    client = _client(config_path)
    entries = client.get("/api/config/routing").json()["entries"]
    assert [(e["backend"], e["model"]) for e in entries] == [
        ("vllm", "qwen2.5-coder"), ("ollama", "llama"), ("vllm", "qwen2.5-coder"),
        ("vllm", "coder-32b"), ("ollama", "llama"),
    ]


def test_the_page_finds_the_duplicates_in_the_servers_read(
    temp_config_with_routing: tuple[Path, Path], tmp_path: Path,
) -> None:
    config_path, routing_path = temp_config_with_routing
    _with_duplicates(routing_path)
    entries = _client(config_path).get("/api/config/routing").json()["entries"]
    served = tmp_path / "served.json"
    served.write_text(json.dumps({
        "entries": entries,
        "expected": [
            {"backend": "vllm", "model": "qwen2.5-coder", "positions": [1, 3]},
            {"backend": "ollama", "model": "llama", "positions": [2, 5]},
        ],
    }))
    _run_node(tmp_path, served)


def test_the_page_finds_nothing_in_a_table_without_duplicates(
    temp_config_with_routing: tuple[Path, Path], tmp_path: Path,
) -> None:
    config_path, _routing_path = temp_config_with_routing
    entries = _client(config_path).get("/api/config/routing").json()["entries"]
    served = tmp_path / "served.json"
    served.write_text(json.dumps({"entries": entries, "expected": []}))
    _run_node(tmp_path, served)


def test_the_duplicate_checks_run_against_the_shipped_functions(tmp_path: Path) -> None:
    output = _run_node(tmp_path)
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", output, re.MULTILINE)
    assert summary is not None and int(summary.group(1)) >= 25


def test_the_shipped_mask_is_the_servers_mask() -> None:
    """The page's mask must be the server's, or the page would send the mask as a value."""
    match = re.search(r"var CFG_SECRET_MASK = '([^']*)';", _CONFIG_JS)
    assert match is not None
    assert match.group(1) == SECRET_MASK


def test_a_new_duplicate_is_still_refused_and_an_unrelated_write_still_lands(
    temp_config_with_routing: tuple[Path, Path],
) -> None:
    config_path, routing_path = temp_config_with_routing
    _with_duplicates(routing_path)
    client = _client(config_path)

    refused = client.post(
        "/api/config/routing/entry",
        json={"entry": _entry("vllm", "qwen2.5-coder"), "base_hash": _routing_hash(client)},
    )
    assert refused.status_code == 422
    assert len(yaml.safe_load(routing_path.read_text())["entries"]) == 5

    applied = client.put(
        "/api/config/routing/entry/3",
        json={"expected": {"backend": "vllm", "model": "coder-32b"},
              "changes": {"model": "coder-34b"}, "base_hash": _routing_hash(client)},
    )
    assert applied.status_code == 200
    assert yaml.safe_load(routing_path.read_text())["entries"][3]["model"] == "coder-34b"
