"""442: the full performer image ships QAre's pinned browser, so browser QA profiles run.

QAre drives browsers through its own playwright-core, which needs the Chromium revision
of its release, found through ``PLAYWRIGHT_BROWSERS_PATH``. These tests read
``agent/performer/Dockerfile.full``; CI's Docker Images job builds it, and the build's own
launch check is what proves the browser starts.
"""

from __future__ import annotations

import re
from pathlib import Path

from performer.stage_jobs.qare import qare_env

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "agent/performer/Dockerfile.full"
BROWSERS = "/opt/qare/drivers/web/browsers"


def _text() -> str:
    return DOCKERFILE.read_text()


def _instructions() -> list[str]:
    """Each instruction on one line, as Docker reads it: comments dropped, continuations joined."""
    out: list[str] = []
    current = ""
    for raw in _text().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        if raw.endswith("\\"):
            current += raw[:-1] + " "
            continue
        current += raw
        if current.strip():
            out.append(current.strip())
        current = ""
    return out


def _arg(name: str) -> str:
    for line in _instructions():
        match = re.fullmatch(rf"ARG {name}=(\S+)", line)
        if match:
            return match.group(1)
    raise AssertionError(f"no ARG {name} default in Dockerfile.full")


def test_one_qare_image_is_pinned_and_it_is_qare_web_at_the_qare_version() -> None:
    image = _arg("QARE_IMAGE")
    match = re.fullmatch(r"ghcr\.io/vividynamics/qare-web:([^@]+)@sha256:[0-9a-f]{64}", image)
    assert match, image
    assert match.group(1) == _arg("QARE_VERSION"), "the browser image must be the QAre release that runs"
    assert "FROM ${QARE_IMAGE} AS qare" in _instructions()
    # Bumping QAre is one change: no second QAre image (qare-core) to keep in step.
    assert "qare-core:" not in _text()
    assert sum("vividynamics/qare-" in line for line in _instructions()) == 1


def test_qares_tree_with_its_browsers_comes_from_that_image() -> None:
    assert "COPY --from=qare /opt/qare /opt/qare" in _instructions()
    assert _arg("QARE_BROWSERS_PATH") == BROWSERS


def test_the_qare_on_path_points_its_playwright_at_qares_browsers() -> None:
    wrapper = next(line for line in _instructions() if "> /usr/local/bin/qare" in line)
    assert '"PLAYWRIGHT_BROWSERS_PATH=${QARE_BROWSERS_PATH}"' in wrapper
    assert "'export PLAYWRIGHT_BROWSERS_PATH'" in wrapper
    assert """'exec /opt/qare/bin/qare "$@"'""" in wrapper
    assert 'test "$(qare --version)" = "${QARE_VERSION}"' in wrapper


def test_the_stage_job_would_not_hand_qare_an_image_wide_browser_path() -> None:
    # Why the path lives in the wrapper: QAre gets an allow-list of the job's environment.
    assert "PLAYWRIGHT_BROWSERS_PATH" not in qare_env({"PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "PATH": "/bin"}, [])


def test_no_image_env_moves_the_python_playwright_chromium() -> None:
    # The performer's own Python Playwright keeps its unpinned Chromium in the default path.
    assert not any(line.startswith("ENV") and "PLAYWRIGHT_BROWSERS_PATH" in line for line in _instructions())
    assert any(line.startswith("RUN pip install --no-cache-dir playwright") for line in _instructions())


def test_the_browsers_system_libraries_come_from_qares_own_playwright() -> None:
    assert _arg("QARE_PLAYWRIGHT") == "/opt/qare/lib/packages/core/node_modules/playwright-core"
    deps = next(line for line in _instructions() if "install-deps chromium" in line)
    assert 'node "${QARE_PLAYWRIGHT}/cli.js" install-deps chromium' in deps
    assert "xvfb" in deps and "libgtk-3-0" in deps


def test_the_build_launches_qares_browser_headless_and_loads_about_blank() -> None:
    check = next(line for line in _instructions() if "chromium.launch" in line)
    assert check.startswith("RUN ")
    # The path the check uses is the one the wrapper sets, in a clean environment.
    assert "sed -n 's/^PLAYWRIGHT_BROWSERS_PATH=//p' /usr/local/bin/qare" in check
    assert "env -i " in check
    assert "require(root)" in check and 'process.env.QARE_PLAYWRIGHT' in check
    assert "launch({ headless: true })" in check
    assert 'page.goto("about:blank")' in check
    assert 'if (url !== "about:blank") throw' in check
    assert "process.exit(1)" in check
    # The browsers must be the ones built for this playwright-core.
    assert "/opt/qare/drivers/web/DRIVER.json" in check
    assert "PLAYWRIGHT_BROWSERS_PATH + \"/\"" in check


def test_the_browser_check_runs_after_qare_is_installed() -> None:
    lines = _instructions()
    copy = lines.index("COPY --from=qare /opt/qare /opt/qare")
    wrapper = next(i for i, line in enumerate(lines) if "> /usr/local/bin/qare" in line)
    check = next(i for i, line in enumerate(lines) if "chromium.launch" in line)
    assert copy < wrapper < check


def test_the_docs_say_which_browser_profiles_run() -> None:
    doc = (ROOT / "docs/stage-providers.md").read_text()
    assert "qare-web" in doc
    assert "#442" in doc and "#435" in doc
    assert "package.json" in doc
