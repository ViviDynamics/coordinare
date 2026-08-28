"""Spec 151 (US2, T012 / C2) — the coordinare→performer dispatch interface carries
the loopback fake host: both github_api_url AND github_graphql_url survive into
JobInitPayload.metadata (and thus into the performer's Score), and WorkspaceManager
builds repo_url from config.git_base_url (bench git:// vs. default https).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinare.config import ProjectConfiguration
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.workspace import WorkspaceInfo, WorkspaceManager

# The performer is a separate package under agent/performer/src — make it importable
# so we can prove the metadata → Score bridge the way the container does it.
_PERFORMER_SRC = Path(__file__).resolve().parents[2] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))


def _config(**overrides) -> ProjectConfiguration:
    base = {
        "github_org": "bench-org", "project_name": "bench-repo",
        "human_reviewers": ["reviewer1"], "github_token": "fake-token",
    }
    base.update(overrides)
    return ProjectConfiguration(**base)


def test_both_urls_survive_into_metadata_and_score() -> None:
    # card_context as the dispatch node builds it (github_api_url + github_graphql_url).
    card_context = {
        "id": "PVTI_1",
        "role": "implementing",
        "github_api_url": "http://127.0.0.1:5555",
        "github_graphql_url": "http://127.0.0.1:5555/graphql",
    }
    svc = HTTPPerformerService(
        PerformerEndpointConfig(id="e", mode="ephemeral", roles=["implementer"], image="img")
    )
    ws = WorkspaceInfo(path=None, branch="bench/x", repo_url="git://127.0.0.1:9418/bench-org/bench-repo.git")
    payload = svc._build_job_payload(card_context, ws)

    assert payload.metadata["github_api_url"] == "http://127.0.0.1:5555"
    assert payload.metadata["github_graphql_url"] == "http://127.0.0.1:5555/graphql"
    # The git:// bench remote transports through JobInitPayload (was HttpUrl-only).
    assert payload.repo_url == "git://127.0.0.1:9418/bench-org/bench-repo.git"

    # The performer spreads metadata into the Score dict (main.py _perform_job);
    # both URL overrides land on the Score (repo scheme is a separate concern — T013).
    from performer.models import Score

    score = Score(**{**payload.metadata, "title": "t", "repo_url": "https://github.com/o/r.git", "branch": payload.branch})
    assert score.github_api_url == "http://127.0.0.1:5555"
    assert score.github_graphql_url == "http://127.0.0.1:5555/graphql"


async def test_workspace_repo_url_from_git_base_url_bench() -> None:
    cfg = _config(git_base_url="git://127.0.0.1:9418", agent_transport="kubernetes")
    wm = WorkspaceManager(cfg, github_service=None)
    info = await wm.prepare({"id": "PVTI_1", "title": "Do a thing"})
    assert info.repo_url == "git://127.0.0.1:9418/bench-org/bench-repo.git"


async def test_workspace_repo_url_default_is_real_github() -> None:
    cfg = _config(agent_transport="kubernetes")  # git_base_url defaults to https://github.com
    wm = WorkspaceManager(cfg, github_service=None)
    info = await wm.prepare({"id": "PVTI_1", "title": "Do a thing"})
    assert info.repo_url == "https://github.com/bench-org/bench-repo.git"


def test_git_base_url_rejects_non_git_schemes() -> None:
    # WorkspaceManager formats git_base_url straight into the HOST-side `git clone`
    # without passing through Score, so the scheme is enforced at config load.
    for bad in ("file:///tmp/repo", "ssh://git@example.com", "ftp://example.com"):
        with pytest.raises(ValueError, match="must use https:// or git://"):
            _config(git_base_url=bad)
    # The container-facing variant is held to the same rule.
    with pytest.raises(ValueError, match="must use https:// or git://"):
        _config(performer_git_base_url="file:///tmp/repo")
    # None stays None (falls back to git_base_url in WorkspaceManager).
    assert _config().performer_git_base_url is None
