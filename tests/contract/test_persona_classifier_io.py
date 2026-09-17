"""Spec 074 — Contract tests for the persona-scope classifier I/O.

These tests pin the *wire format* between coordinare and the classifier LLM as
documented in ``specs/074-persona-scope-tiering/contracts/classifier-prompt.md``.
The classifier never sees the raw diff body (FR-002 / R1) — only path stats,
file status, path-class memberships, and project context.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from coordinare.services import persona_classifier


class _FakeBackend:
    """Captures the prompt body passed to ``prompt(...)`` for inspection."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def prompt(self, body: str, response_format: str | None = None) -> dict[str, Any]:
        self.calls.append({"body": body, "response_format": response_format})
        return self._response


class _FakeGitHub:
    def __init__(self, files: list[dict[str, Any]], head_sha: str = "abc123") -> None:
        self._files = files
        self._head_sha = head_sha

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {"files": list(self._files), "head_sha": self._head_sha}


class _ScopeCfg:
    def __init__(
        self,
        *,
        enabled: bool = True,
        path_classes: dict[str, list[str]] | None = None,
        classifier_latency_budget_seconds: float = 30.0,
    ) -> None:
        self.enabled = enabled
        self.path_classes = path_classes or {
            "docs": ["*.md", "docs/**/*"],
            "runtime": ["src/**/*.py"],
            "tests": ["tests/**/*", "**/test_*.py"],
            "security_sensitive": ["src/auth/*.py", "src/auth/**/*.py"],
        }
        self.classifier_latency_budget_seconds = classifier_latency_budget_seconds


class _Cfg:
    def __init__(self, **kw: Any) -> None:
        self.persona_scope = _ScopeCfg(**kw)


def _card() -> dict[str, Any]:
    return {
        "id": "PVI_42",
        "title": "Add token-refresh edge-case handling",
        "body": "Refactor session.refresh() to retry once on stale-credential errors.",
        "pr_url": "https://github.com/acme/repo/pull/91",
    }


def _pr_files_mixed() -> list[dict[str, Any]]:
    return [
        {"path": "src/auth/session.py", "added": 12, "removed": 3, "status": "modified"},
        {"path": "tests/unit/test_session.py", "added": 8, "removed": 0, "status": "modified"},
        {"path": "README.md", "added": 4, "removed": 1, "status": "modified"},
    ]


def _full_response() -> dict[str, Any]:
    return {
        "data": {
            "personas": {
                "reviewer": {"depth": "normal", "focus": "auth refactor"},
                "security": {"depth": "full", "focus": "session.refresh path"},
                "qa": {"depth": "normal", "focus": "retry behavior tests"},
                "tech_writer": {"depth": "skim", "focus": "minor README"},
                "closer": {"depth": "skim", "focus": "tag scope-invariant"},
            },
        },
    }


def _extract_input_json(body: str) -> dict[str, Any]:
    m = re.search(r"<input>\s*(\{.*\})\s*</input>", body, flags=re.DOTALL)
    assert m, "rendered prompt missing <input>...</input> JSON block"
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# Input schema shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_schema_shape() -> None:
    backend = _FakeBackend(_full_response())
    gh = _FakeGitHub(_pr_files_mixed())

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 1},
        card=_card(),
        conducting_backend=backend,
        config=_Cfg(),
        github_service=gh,
        workspace_path=None,
        classifier_model="test/model",
    )

    assert scope is not None
    assert len(backend.calls) == 1
    data = _extract_input_json(backend.calls[0]["body"])

    # Top-level keys per contracts/classifier-prompt.md.
    for key in (
        "card",
        "pr",
        "project_context",
        "personas",
        "path_classes",
        "depth_definitions",
    ):
        assert key in data, f"missing top-level field: {key}"

    # card.{id,title,summary}
    assert data["card"]["id"] == "PVI_42"
    assert data["card"]["title"]
    assert "summary" in data["card"]

    # pr.{number,head_sha,files[*]}
    assert data["pr"]["number"] == 91
    assert data["pr"]["head_sha"] == "abc123"
    files = data["pr"]["files"]
    assert isinstance(files, list) and files
    for f in files:
        for k in ("path", "added", "removed", "status", "classes"):
            assert k in f, f"file entry missing {k}"
        assert isinstance(f["classes"], list)

    # Path classification produced expected memberships.
    by_path = {f["path"]: f for f in files}
    assert "runtime" in by_path["src/auth/session.py"]["classes"]
    assert "security_sensitive" in by_path["src/auth/session.py"]["classes"]
    assert "tests" in by_path["tests/unit/test_session.py"]["classes"]
    assert "docs" in by_path["README.md"]["classes"]

    # personas list verbatim from CLASSIFIER_PERSONAS.
    assert data["personas"] == list(persona_classifier.CLASSIFIER_PERSONAS)
    # depth_definitions verbatim.
    assert data["depth_definitions"] == persona_classifier.DEPTH_DEFINITIONS
    # path_classes echoed from config.
    assert "docs" in data["path_classes"]


# ---------------------------------------------------------------------------
# Output schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_schema_validates() -> None:
    backend = _FakeBackend(
        {
            "data": {
                "personas": {
                    "reviewer": {"depth": "skim", "focus": "docs only"},
                    "security": {"depth": "skip", "focus": "no sensitive paths"},
                    "qa": {"depth": "skip", "focus": "no runtime code"},
                    "tech_writer": {"depth": "full", "focus": "doc changes"},
                    "closer": {"depth": "normal", "focus": "tag invariant"},
                },
            },
        },
    )
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=_Cfg(),
        github_service=gh,
        classifier_model="test/model",
    )

    assert scope is not None
    personas = scope["personas"]
    valid_depths = persona_classifier.VALID_DEPTHS
    for name in persona_classifier.CLASSIFIER_PERSONAS:
        assert name in personas, f"missing persona: {name}"
        slice_ = personas[name]
        assert set(slice_.keys()) >= {"depth", "focus", "overrides"}
        assert slice_["depth"] in valid_depths
        assert isinstance(slice_["focus"], str)
        assert isinstance(slice_["overrides"], list)

    # FR-009: closer is scope-invariant — depth forced to full + tagged.
    assert personas["closer"]["depth"] == "full"
    assert "closer_is_scope_invariant" in personas["closer"]["overrides"]


# ---------------------------------------------------------------------------
# Classifier never sees the raw diff body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifier_never_sees_raw_diff() -> None:
    """FR-002 / R1: the rendered prompt must contain only path stats + project
    context, never the diff body.
    """
    backend = _FakeBackend(_full_response())
    gh = _FakeGitHub(_pr_files_mixed())

    await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=_Cfg(),
        github_service=gh,
        classifier_model="test/model",
    )

    assert len(backend.calls) == 1
    body = backend.calls[0]["body"]

    # Unified-diff markers must NOT appear.
    for marker in ("@@", "diff --git", "+++ b/", "--- a/", "\n+ ", "\n- "):
        assert marker not in body, f"raw diff marker leaked into prompt: {marker!r}"

    # The path stats DO appear (sanity: the contract IS sending stats).
    assert "src/auth/session.py" in body
    assert "added" in body and "removed" in body and "status" in body
