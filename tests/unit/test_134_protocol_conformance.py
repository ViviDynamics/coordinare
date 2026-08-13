"""Spec 134 — GitHubServiceProtocol conformance.

The Protocol in ``graph/state.py`` is the single documented contract for the
GitHub-service surface the coordinare lifecycle depends on. This test proves the
real ``GitHubService`` satisfies it (so completing the Protocol was pure
interface capture — no behaviour change), and is the guard that keeps the
Protocol and the real class from drifting. The fake's conformance is added in
``test_134_fake_github.py`` once the fake exists.
"""

from __future__ import annotations

import inspect

from coordinare.graph.state import GitHubServiceProtocol
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.github import GitHubService


def _protocol_members() -> frozenset[str]:
    # __protocol_attrs__ (py3.12+) is the exact set of members isinstance()
    # considers — our declared methods incl. the private _current_token, minus
    # dunders/typing internals. Deriving from it means this test cannot drift
    # from the Protocol definition.
    members = getattr(GitHubServiceProtocol, "__protocol_attrs__", None)
    assert members, "GitHubServiceProtocol should expose __protocol_attrs__ (py3.12+)"
    return frozenset(members)


def test_protocol_covers_a_substantial_surface() -> None:
    # Guards against someone silently shrinking the contract back to the old
    # 7-method stub.
    assert len(_protocol_members()) >= 30


def test_real_github_service_satisfies_protocol() -> None:
    missing = [name for name in _protocol_members() if not hasattr(GitHubService, name)]
    assert not missing, f"GitHubService is missing Protocol members: {sorted(missing)}"


def test_every_protocol_member_is_async_on_real_service() -> None:
    not_async = [
        name
        for name in _protocol_members()
        if not inspect.iscoroutinefunction(getattr(GitHubService, name))
    ]
    assert not not_async, f"GitHubService members not declared async: {sorted(not_async)}"


def test_private_current_token_is_part_of_the_contract() -> None:
    # A few nodes reach for _current_token() via hasattr for the rebase token;
    # it must stay in the contract so the fake is required to provide it.
    assert "_current_token" in _protocol_members()
    assert inspect.iscoroutinefunction(GitHubService._current_token)


def test_fake_github_service_satisfies_protocol() -> None:
    missing = [name for name in _protocol_members() if not hasattr(FakeGitHubService, name)]
    assert not missing, f"FakeGitHubService is missing Protocol members: {sorted(missing)}"


def test_every_protocol_member_is_async_on_fake() -> None:
    not_async = [
        name
        for name in _protocol_members()
        if not inspect.iscoroutinefunction(getattr(FakeGitHubService, name))
    ]
    assert not not_async, f"FakeGitHubService members not declared async: {sorted(not_async)}"
