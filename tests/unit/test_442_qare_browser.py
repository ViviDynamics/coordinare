"""442: the full performer image ships QAre's pinned browser, so browser QA profiles run.

QAre drives browsers through its own playwright-core, which needs the Chromium revision
of its release. The image links that revision into Playwright's default cache, beside the
Python Playwright's. These tests read
``agent/performer/Dockerfile.full``; CI's Docker Images job builds it, and the build's own
launch check is what proves the browser starts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from performer.stage_jobs.qare import BROWSER_CACHE_ENV, qare_env
from pydantic import ValidationError

from coordinare.stage_provider_config import _QARE_BROWSER_CACHE_ENV, QAreProviderConfig

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "agent/performer/Dockerfile.full"


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
            current += raw[:-1]  # Docker joins continuations with nothing in between
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


def _links() -> str:
    return next(line for line in _instructions() if "qare_browsers=/opt/qare/drivers/web/browsers" in line)


def test_qares_browsers_sit_beside_the_python_ones_in_playwrights_default_cache() -> None:
    links = _links()
    assert "cache=/root/.cache/ms-playwright" in links
    # Each revision directory is linked, never copied (no second 700 MB), and one the
    # cache already holds (the same revision from the Python Playwright) is left alone.
    assert 'ln -s "${dir%/}" "${cache}/${name}"' in links
    assert 'if [ ! -e "${cache}/${name}" ] && [ ! -L "${cache}/${name}" ]' in links
    # An empty glob stops the build with a reason, not a link named "*".
    assert '[ -d "${dir}" ] || { echo "no browser directories under ${qare_browsers}" >&2; exit 1; }' in links
    # QAre's .links entry keeps a later `playwright install` from deleting its revisions,
    # so each entry must name a playwright-core in this image.
    assert '[ -f "${target}/browsers.json" ] ||' in links
    assert 'cp "${link}" "${cache}/.links/"' in links
    # And the build runs that cleanup before the launch check, so CI proves they survive it.
    assert links.rstrip().endswith("&& playwright install chromium")


def test_no_browser_path_is_set_for_qare_or_anything_it_starts() -> None:
    # QAre passes its environment to every check and app it starts: a path set there
    # would hide the Python Playwright's revision from them.
    assert "PLAYWRIGHT_BROWSERS_PATH" not in "\n".join(_instructions())
    assert "RUN ln -s /opt/qare/bin/qare /usr/local/bin/qare && " + (
        'test "$(qare --version)" = "${QARE_VERSION}"'
    ) in [" ".join(line.split()) for line in _instructions()]


def test_the_stage_job_gives_qare_home_so_it_finds_the_default_cache() -> None:
    env = qare_env({"HOME": "/root", "PATH": "/bin"}, [])
    assert env["HOME"] == "/root"


@pytest.mark.parametrize("name", ["XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"])
def test_the_stage_job_never_hands_qare_a_moved_browser_cache(name: str) -> None:
    # Either variable moves where QAre's Playwright looks, away from the linked revisions,
    # whether the image set it or an operator named it.
    env = {"HOME": "/root", "PATH": "/bin", name: "/elsewhere"}
    assert name not in qare_env(env, [])
    assert name not in qare_env(env, [name])


def test_the_config_refuses_exactly_the_names_the_stage_job_drops() -> None:
    assert _QARE_BROWSER_CACHE_ENV == BROWSER_CACHE_ENV == {"XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"}


@pytest.mark.parametrize("name", ["XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"])
def test_config_refuses_passing_a_browser_cache_location_to_qare(name: str) -> None:
    with pytest.raises(ValidationError, match=name):
        QAreProviderConfig(kind="qare", version="2026.11.44", env_passthrough=["OPENAI_API_KEY", name])
    assert QAreProviderConfig(
        kind="qare", version="2026.11.44", env_passthrough=["OPENAI_API_KEY"],
    ).env_passthrough == ["OPENAI_API_KEY"]


def test_the_python_playwright_chromium_is_kept() -> None:
    assert any(line.startswith("RUN pip install --no-cache-dir playwright") for line in _instructions())


def test_the_browsers_system_libraries_come_from_qares_own_playwright() -> None:
    deps = next(line for line in _instructions() if "install-deps chromium" in line)
    assert "node /opt/qare/lib/packages/core/node_modules/playwright-core/cli.js install-deps chromium" in deps
    assert "xvfb" in deps and "libgtk-3-0" in deps


def _check() -> str:
    return next(line for line in _instructions() if "chromium.launch" in line)


def test_the_build_launches_qares_browser_headless_and_loads_about_blank() -> None:
    check = _check()
    assert check.startswith("RUN env -i PATH=\"${PATH}\" HOME=/root node -e ")
    assert 'const root = "/opt/qare/lib/packages/core/node_modules/playwright-core";' in check
    assert "const pw = require(root);" in check
    assert "launch({ headless: true })" in check
    assert 'page.goto("about:blank")' in check
    assert 'if (url !== "about:blank") throw' in check
    assert "process.exit(1)" in check
    # The browsers must be the ones built for this playwright-core, found in the default cache.
    assert "/opt/qare/drivers/web/DRIVER.json" in check
    assert 'exe.startsWith("/root/.cache/ms-playwright/")' in check


def test_the_build_check_also_launches_the_python_playwright_chromium() -> None:
    check = _check()
    assert 'HOME=/root python3 -c \'from playwright.sync_api import sync_playwright;' in check
    assert "launch(headless=True)" in check
    assert 'assert pg.url == "about:blank"' in check


def test_the_python_check_compiles_as_docker_hands_it_to_python() -> None:
    check = _check()
    code = check[check.index("python3 -c '") + len("python3 -c '"): check.rindex("'")]
    compile(code, "<python3 -c>", "exec")


def test_the_browser_check_runs_after_qare_and_its_browsers_are_in_place() -> None:
    lines = _instructions()
    python = next(i for i, line in enumerate(lines) if line.startswith("RUN pip install --no-cache-dir playwright"))
    copy = lines.index("COPY --from=qare /opt/qare /opt/qare")
    deps = next(i for i, line in enumerate(lines) if "install-deps chromium" in line)
    links = lines.index(_links())
    check = lines.index(_check())
    assert python < copy < deps < links < check


def test_the_docs_say_which_browser_profiles_run() -> None:
    doc = (ROOT / "docs/stage-providers.md").read_text()
    assert "qare-web" in doc
    assert "#442" in doc and "#435" in doc
    assert "package.json" in doc
