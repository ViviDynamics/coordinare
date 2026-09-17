"""Integration test for spec-092 US3: configured-but-missing test-env file.

A *configured* ``test_env`` block that points at an absent file is an operator error,
not a transient condition. The loader (and the shared ``resolve_test_env_vars`` resolver)
must surface a distinct coordinare-side :class:`TestEnvFileError` that names the resolved
path AND the originating field (``repo_path`` / ``host_path``) — never a silent empty
dict (which would let a stale ``services-start.sh`` survive) and never a buried, opaque
``exit 75`` from the dry-run downstream.

Contrast with the agent-DISCOVERED fallback (US2): a vanished discovered file is
best-effort (returns ``{}``) because it was never an explicit operator promise — see
``test_test_env_fallback.py::test_no_source_still_trips_exit_75``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.config import TestEnvConfig
from coordinare.services.env_cache import resolve_test_env_vars
from coordinare.services.test_env_loader import TestEnvFileError, load_test_env

ORG = "vivi-org"
REPO = "website"


class _FakeGitHub:
    """GitHub content seam: returns ``None`` for an absent repo file."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.calls: list[tuple[str, str, str]] = []

    async def get_file_content(self, org: str, repo: str, path: str) -> str | None:
        self.calls.append((org, repo, path))
        return self._files.get(path)


class TestConfiguredMissingFileErrors:
    def test_host_path_missing_raises_naming_path_and_field(
        self, tmp_path: Path,
    ) -> None:
        absent = tmp_path / "does-not-exist.env"

        with pytest.raises(TestEnvFileError) as excinfo:
            load_test_env(TestEnvConfig(host_path=str(absent)), repo_root=tmp_path)

        msg = str(excinfo.value)
        assert str(absent) in msg  # the resolved path is named.
        assert "host_path" in msg  # the originating field is named.

    def test_repo_path_missing_on_disk_raises_naming_path_and_field(
        self, tmp_path: Path,
    ) -> None:
        repo_root = tmp_path / "symphony_repo"
        repo_root.mkdir()
        # repo_path points inside the clone, but the file was never written.

        with pytest.raises(TestEnvFileError) as excinfo:
            load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=repo_root)

        msg = str(excinfo.value)
        assert str(repo_root / ".env.test") in msg
        assert "repo_path" in msg

    async def test_repo_path_absent_in_repo_raises_via_resolver(self) -> None:
        # The coordinare has no on-disk clone at dispatch: a repo_path source is fetched
        # through the GitHub API. An absent file there is still a hard config error.
        github = _FakeGitHub({})  # the configured file is not in the repo.

        with pytest.raises(TestEnvFileError) as excinfo:
            await resolve_test_env_vars(
                symphony_name="website",
                test_env=TestEnvConfig(repo_path="config/.env.test"),
                github_org=ORG,
                repo=REPO,
                github_service=github,
            )

        msg = str(excinfo.value)
        assert "config/.env.test" in msg  # resolved path named.
        assert "repo_path" in msg  # originating field named.
        # It tried to fetch — i.e. it did not silently short-circuit to {}.
        assert github.calls == [(ORG, REPO, "config/.env.test")]
