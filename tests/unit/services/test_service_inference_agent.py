"""Unit tests for spec 063 T011 service-inference agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from coordinare_service_inference.agent import (
    AgentError,
    IterationBudgetExceeded,
    LLMStep,
    ManifestValidationError,
    ServiceInferenceAgent,
    ToolCall,
    ToolCallBudgetExceeded,
    UnknownToolError,
)
from coordinare_service_inference.tools import ToolSandbox


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "database.yml").write_text("adapter: postgresql\n")
    return tmp_path


@pytest.fixture()
def sandbox(project: Path) -> ToolSandbox:
    return ToolSandbox.for_root(project)


class _StubClient:
    """Walks a pre-baked script of :class:`LLMStep` values."""

    def __init__(self, script: list[LLMStep]) -> None:
        self._script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep:
        self.calls.append(messages)
        if not self._script:
            raise AssertionError("stub client exhausted")
        return self._script.pop(0)


def _manifest_dict() -> dict[str, Any]:
    return {
        "services": [
            {
                "name": "redis",
                "binary": "/usr/bin/redis-server",
                "version": "7.2",
                "data_dir": "/tmp/redis",
                "port": 6379,
                "why_needed": "rails session store",
                "sources": ["Gemfile"],
            }
        ],
        "cache_inputs": ["Gemfile", "config/database.yml"],
        "agent_version": "test-0",
    }


@pytest.mark.asyncio
async def test_terminates_on_manifest(sandbox: ToolSandbox) -> None:
    client = _StubClient([LLMStep(manifest=_manifest_dict())])
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    manifest = await agent.run("infer services")
    assert manifest.services[0].name == "redis"
    assert client.calls, "client should have been stepped at least once"


@pytest.mark.asyncio
async def test_dispatches_tool_calls_then_terminates(sandbox: ToolSandbox) -> None:
    client = _StubClient(
        [
            LLMStep(tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "Gemfile"})]),
            LLMStep(manifest=_manifest_dict()),
        ]
    )
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    manifest = await agent.run("infer services")
    assert manifest.services[0].name == "redis"
    # The second LLM step must have received the tool_result from turn 1.
    second_turn_messages = client.calls[1]
    last = second_turn_messages[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert "rails" in last["content"][0]["content"]["content"]


@pytest.mark.asyncio
async def test_invalid_manifest_raises(sandbox: ToolSandbox) -> None:
    bad = {"services": [{"name": "x"}], "cache_inputs": [], "agent_version": "x"}
    client = _StubClient([LLMStep(manifest=bad)])
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    with pytest.raises(ManifestValidationError):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_tool_call_budget_enforced(sandbox: ToolSandbox) -> None:
    # Three calls in one step; budget=2 → reject before any dispatch.
    client = _StubClient(
        [
            LLMStep(
                tool_calls=[
                    ToolCall(id=f"t{i}", name="list_dir", arguments={"path": "."})
                    for i in range(3)
                ]
            )
        ]
    )
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client, max_tool_calls=2)
    with pytest.raises(ToolCallBudgetExceeded):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_tool_call_budget_cumulative_across_steps(sandbox: ToolSandbox) -> None:
    # 2 + 1 calls over two steps with budget=2 → second step's call exceeds budget.
    client = _StubClient(
        [
            LLMStep(
                tool_calls=[
                    ToolCall(id="a", name="list_dir", arguments={"path": "."}),
                    ToolCall(id="b", name="list_dir", arguments={"path": "config"}),
                ]
            ),
            LLMStep(tool_calls=[ToolCall(id="c", name="list_dir", arguments={"path": "."})]),
        ]
    )
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client, max_tool_calls=2)
    with pytest.raises(ToolCallBudgetExceeded):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_sandbox_traversal_is_surfaced_not_crashed(
    sandbox: ToolSandbox,
) -> None:
    """A ``..`` traversal must reach the LLM as a tool_result error, not raise."""
    client = _StubClient(
        [
            LLMStep(
                tool_calls=[
                    ToolCall(id="t1", name="read_file", arguments={"path": "../etc/passwd"})
                ]
            ),
            LLMStep(manifest=_manifest_dict()),
        ]
    )
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    await agent.run("infer")
    second_turn = client.calls[1][-1]
    result_block = second_turn["content"][0]
    assert result_block["is_error"] is True
    assert result_block["content"]["error"] == "sandbox_violation"


@pytest.mark.asyncio
async def test_symlink_escape_surfaced(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        os.symlink(outside, project / "leak.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    sb = ToolSandbox.for_root(project)
    client = _StubClient(
        [
            LLMStep(
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "leak.txt"})]
            ),
            LLMStep(manifest=_manifest_dict()),
        ]
    )
    agent = ServiceInferenceAgent(sandbox=sb, client=client)
    await agent.run("infer")
    assert client.calls[1][-1]["content"][0]["content"]["error"] == "sandbox_violation"


@pytest.mark.asyncio
async def test_unknown_tool_raises(sandbox: ToolSandbox) -> None:
    client = _StubClient(
        [LLMStep(tool_calls=[ToolCall(id="t1", name="exec_shell", arguments={})])]
    )
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    with pytest.raises(UnknownToolError):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_empty_step_raises(sandbox: ToolSandbox) -> None:
    client = _StubClient([LLMStep()])
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    with pytest.raises(AgentError):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_iteration_budget_exceeded(sandbox: ToolSandbox) -> None:
    # Each step makes a single tool call — under tool-call budget but never produces
    # a manifest, so the iteration cap is what stops the loop.
    script = [
        LLMStep(tool_calls=[ToolCall(id=f"t{i}", name="list_dir", arguments={})])
        for i in range(10)
    ]
    client = _StubClient(script)
    agent = ServiceInferenceAgent(
        sandbox=sandbox, client=client, max_iterations=3, max_tool_calls=100
    )
    with pytest.raises(IterationBudgetExceeded):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_unresolvable_binary_rejected(sandbox: ToolSandbox) -> None:
    bad = {
        "services": [
            {
                "name": "wat",
                "binary": "definitely-not-on-path-wat",
                "version": "1.0",
                "data_dir": "/tmp/wat",
                "port": 4242,
                "why_needed": "test",
            }
        ],
        "cache_inputs": ["Gemfile"],
        "agent_version": "test-0",
    }
    client = _StubClient([LLMStep(manifest=bad)])
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    with pytest.raises(ManifestValidationError, match="not resolvable"):
        await agent.run("infer")


@pytest.mark.asyncio
async def test_unresolvable_binary_allowed_for_external(sandbox: ToolSandbox) -> None:
    external = {
        "services": [
            {
                "name": "rds",
                "binary": "psql-cli-not-installed",
                "version": "16",
                "data_dir": "/tmp/rds",
                "port": 5432,
                "why_needed": "test",
                "external_required": True,
                "required_env_vars": ["DATABASE_URL"],
            }
        ],
        "cache_inputs": ["Gemfile"],
        "agent_version": "test-0",
    }
    client = _StubClient([LLMStep(manifest=external)])
    agent = ServiceInferenceAgent(sandbox=sandbox, client=client)
    manifest = await agent.run("infer")
    assert manifest.services[0].external_required is True
