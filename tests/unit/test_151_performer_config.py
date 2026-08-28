"""Spec 151 (US2, T013) — the performer applies both GitHub URL overrides with
the same https-any / http-loopback-only rule (no non-loopback-http leak), and
accepts a git:// repo_url ONLY when ALLOW_INSECURE_REPO_URL is set.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PERFORMER_SRC = Path(__file__).resolve().parents[2] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))

from performer.config import Settings, apply_github_url_override  # noqa: E402
from performer.models import Score  # noqa: E402


@pytest.mark.parametrize("field", ["GITHUB_API_URL", "GITHUB_GRAPHQL_URL"])
def test_http_loopback_accepted(field: str) -> None:
    s = Settings()
    apply_github_url_override("http://127.0.0.1:5555", field, s)
    assert getattr(s, field) == "http://127.0.0.1:5555"


@pytest.mark.parametrize("field", ["GITHUB_API_URL", "GITHUB_GRAPHQL_URL"])
def test_http_non_loopback_rejected_falls_back(field: str) -> None:
    s = Settings()
    original = getattr(s, field)
    apply_github_url_override("http://evil.example.com", field, s)
    # Rejected → default retained (no real-GitHub leak via a spoofed http host).
    assert getattr(s, field) == original


@pytest.mark.parametrize("field", ["GITHUB_API_URL", "GITHUB_GRAPHQL_URL"])
def test_http_host_gateway_rejected_without_optin(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALLOW_HOST_GATEWAY_GITHUB", raising=False)
    s = Settings()
    original = getattr(s, field)
    apply_github_url_override("http://host.docker.internal:5555", field, s)
    # Opt-in absent → treated like any non-loopback http host: rejected.
    assert getattr(s, field) == original


@pytest.mark.parametrize("field", ["GITHUB_API_URL", "GITHUB_GRAPHQL_URL"])
def test_http_host_gateway_accepted_with_optin(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_HOST_GATEWAY_GITHUB", "1")
    s = Settings()
    apply_github_url_override("http://host.docker.internal:5555", field, s)
    assert getattr(s, field) == "http://host.docker.internal:5555"


@pytest.mark.parametrize("field", ["GITHUB_API_URL", "GITHUB_GRAPHQL_URL"])
def test_https_any_accepted(field: str) -> None:
    s = Settings()
    apply_github_url_override("https://ghes.internal/api/v3", field, s)
    assert getattr(s, field) == "https://ghes.internal/api/v3"


def test_repo_url_git_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_INSECURE_REPO_URL", raising=False)
    with pytest.raises(ValueError, match="repo_url must be an HTTPS URL"):
        Score(title="t", repo_url="git://127.0.0.1:9418/bench-org/bench-repo.git", branch="b")


def test_repo_url_git_admitted_when_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_INSECURE_REPO_URL", "1")
    score = Score(title="t", repo_url="git://127.0.0.1:9418/bench-org/bench-repo.git", branch="b")
    assert score.repo_url == "git://127.0.0.1:9418/bench-org/bench-repo.git"


def test_https_repo_url_always_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_INSECURE_REPO_URL", raising=False)
    score = Score(title="t", repo_url="https://github.com/o/r.git", branch="b")
    assert score.repo_url.endswith("/o/r.git")
