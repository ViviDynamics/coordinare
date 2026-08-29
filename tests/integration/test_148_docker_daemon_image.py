"""Spec 148 / issue #223 — the Docker deployment path actually works.

**Why this exists.** `bin/start --container` is the documented way to run
coordinare under Docker, and until now nothing built or ran that image. It was
unbuildable for months — ``uv.lock`` resolves a local path dependency that
``Dockerfile.daemon`` never copied — and the break went unobserved because no
test, workflow, or check exercised the path. The bug was the symptom; the absent
coverage was the defect.

The asymmetry it left is backwards: the Kubernetes path gained rendering tests
and a live cluster install in the very PR that discovered the Docker path had
none, while Docker is the path most self-hosters try first.

**Skipped, not failed, when Docker is unavailable**, and the skip names what to
start. These build a real image, so they are slower than a unit test and belong
here rather than in the unit suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

IMAGE = "coordinare-daemon:test-148"
DOCKERFILE = "Dockerfile.daemon"

SKIP_REASON = (
    "Docker is not available. These tests build the coordinare daemon image.\n"
    "  Start Docker Desktop, or run: colima start"
)


def _docker(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return _docker("info", timeout=30).returncode == 0


pytestmark = [
    pytest.mark.skipif(not _docker_available(), reason=SKIP_REASON),
    # The suite's default timeout is 30s (pyproject). Building a container image
    # does not fit in that, and the symptom is not a clear "this is slow" — it is
    # eight tests erroring at *setup* on the module-scoped fixture, which reads
    # like something structural rather than a clock.
    pytest.mark.timeout(900),
]


@pytest.fixture(scope="module")
def image() -> str:
    """Build the daemon image from a clean context, exactly as bin/start does.

    The build is the test for FR-001: it is what was broken, and what nothing
    checked. A failure here reproduces the original breakage rather than
    describing it.
    """
    result = _docker("build", "-f", DOCKERFILE, "-t", IMAGE, ".")
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-25:])
        pytest.fail(f"the coordinare daemon image does not build:\n{tail}")
    yield IMAGE
    _docker("image", "rm", "-f", IMAGE, timeout=120)


class TestTheImageBuilds:
    def test_every_path_dependency_is_present_in_the_image(self, image: str) -> None:
        """The exact failure that went unnoticed for months.

        ``uv.lock`` resolves ``coordinare-service-inference`` from ``packages/``. A
        Dockerfile that does not copy it fails with "Distribution not found at:
        file:///app/packages/service_inference" — but only if something builds
        the image, which nothing did.

        Asserted by importing the package rather than by checking the Dockerfile
        text, so it holds however the image comes to contain it.
        """
        result = _docker(
            "run",
            "--rm",
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "-c",
            "import coordinare_service_inference; print('ok')",
        )
        assert result.returncode == 0, (
            f"a path dependency is missing from the image:\n{result.stderr.strip()[-800:]}"
        )

    def test_coordinare_itself_imports(self, image: str) -> None:
        result = _docker(
            "run",
            "--rm",
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "-c",
            "import coordinare; print('ok')",
        )
        assert result.returncode == 0, result.stderr.strip()[-800:]


class TestTheEntrypointWorks:
    """The image is useless if it builds and will not run."""

    def test_the_entrypoint_starts_coordinare(self, image: str) -> None:
        result = _docker("run", "--rm", image, "--help")
        assert result.returncode == 0, result.stderr.strip()[-800:]
        assert "--config" in result.stdout

    def test_it_runs_with_the_workdir_bin_start_overrides(self, image: str) -> None:
        """``bin/start --container`` passes ``-w <host project path>``.

        The entrypoint runs the interpreter directly rather than through ``uv``,
        which means it must not depend on the image's own WORKDIR being current.
        Nothing else covers that combination.

        No bind mount: the directory only has to be somewhere other than /app for
        this to prove anything, and a host path would be resolved against the
        Docker host rather than a containerised CI runner.
        """
        result = _docker("run", "--rm", "-w", "/tmp", image, "--help")
        assert result.returncode == 0, result.stderr.strip()[-800:]

    def test_config_validation_runs_inside_the_container(self, image: str) -> None:
        """A real subcommand doing real work, not just argument parsing.

        The config is written *inside* the container from an environment variable
        rather than bind-mounted from the test's temporary directory. A bind mount
        looks simpler and is wrong here: CI runs on a containerised runner, so a
        host path in ``docker run -v`` is resolved against the Docker host, not
        the runner. The path does not exist there, Docker helpfully creates an
        empty directory, and the mount succeeds while delivering nothing —
        "Config file not found" from a test that passes locally.

        Also exercises the ``${VAR}`` credential indirection the Helm chart
        relies on, so both deployment paths cover it.
        """
        config = (
            "github_org: ViviDynamics\n"
            "project_name: smoke\n"
            "github_project_number: 1\n"
            "human_reviewers: [someone]\n"
            "github_token: ${GITHUB_TOKEN}\n"
        )
        result = _docker(
            "run",
            "--rm",
            "-e",
            "GITHUB_TOKEN=not-a-real-token-validation-only",
            "-e",
            f"SMOKE_CONFIG={config}",
            "--entrypoint",
            "sh",
            image,
            "-c",
            'printf "%s" "$SMOKE_CONFIG" > /tmp/config.yaml '
            "&& /app/.venv/bin/python -m coordinare config validate --config /tmp/config.yaml",
        )
        assert result.returncode == 0, (
            f"config validate failed inside the image:\n{result.stdout[-600:]}\n{result.stderr[-600:]}"
        )
        assert "not-a-real-token-validation-only" not in result.stdout, (
            "the token value was echoed back; a credential must not reach the output "
            "of a command an operator runs and pastes into an issue"
        )


class TestItRunsAsNonRoot:
    """The Helm chart runs this image with runAsNonRoot and a fixed UID.

    Nothing outside the chart asserted the image supports that, so a change
    making the image root-only would break the Kubernetes deployment and be
    caught — if at all — by a cluster test rather than here.
    """

    def test_the_interpreter_runs_under_an_arbitrary_uid(self, image: str) -> None:
        result = _docker(
            "run",
            "--rm",
            "--user",
            "10001:10001",
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "-c",
            "import coordinare; print('ok')",
        )
        assert result.returncode == 0, (
            "the image must run as a non-root UID: the Helm chart sets "
            f"runAsNonRoot with runAsUser 10001.\n{result.stderr.strip()[-800:]}"
        )

    def test_nothing_at_runtime_needs_to_write_into_the_image(self, image: str) -> None:
        """A read-only root filesystem is the strongest form of this check.

        It is why the entrypoint invokes the interpreter directly instead of via
        ``uv``, which wants writable cache and lock paths.
        """
        result = _docker(
            "run",
            "--rm",
            "--read-only",
            "--user",
            "10001:10001",
            "--tmpfs",
            "/tmp",
            image,
            "--help",
        )
        assert result.returncode == 0, (
            f"the image needs to write to its own filesystem at runtime:\n"
            f"{result.stderr.strip()[-800:]}"
        )


class TestTheDeclaredPortsMatchReality:
    """EXPOSE said 9090/9091 for months while coordinare served 8080/8090.

    Documentation-only, so nothing broke — but it is the kind of wrong that sends
    someone debugging the wrong layer, and it survived because nothing compared
    the two.
    """

    def test_expose_matches_the_configured_defaults(self, image: str) -> None:
        import sys

        sys.path.insert(0, "src")
        from coordinare.config import ProjectConfiguration

        fields = ProjectConfiguration.model_fields
        expected = {
            str(fields["health_check_port"].default),
            str(fields["dashboard_port"].default),
        }

        result = _docker("inspect", image, "--format", "{{json .Config.ExposedPorts}}")
        exposed = {p.split("/")[0] for p in (json.loads(result.stdout) or {})}

        assert exposed == expected, (
            f"the image declares ports {sorted(exposed)} but coordinare serves "
            f"{sorted(expected)}. EXPOSE is documentation, so this misleads rather "
            "than breaks — which is exactly why it went unnoticed."
        )

    def test_bin_start_documents_the_same_ports(self) -> None:
        """The comment an operator reads before looking for the dashboard.

        ``Dockerfile.daemon``'s EXPOSE was corrected under #201; this comment was
        missed, and still named the old ports.
        """
        import sys

        sys.path.insert(0, "src")
        from coordinare.config import ProjectConfiguration

        fields = ProjectConfiguration.model_fields
        health = str(fields["health_check_port"].default)
        dashboard = str(fields["dashboard_port"].default)

        text = Path("bin/start").read_text()
        for line in text.splitlines():
            if "Health (" not in line:
                continue
            assert health in line and dashboard in line, (
                f"bin/start documents the wrong ports: {line.strip()!r} — "
                f"coordinare serves health {health}, dashboard {dashboard}"
            )
            return
        pytest.fail("bin/start no longer documents its ports; update this test with it")
