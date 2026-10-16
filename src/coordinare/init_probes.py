from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

from coordinare.config import ProjectConfiguration
from coordinare.doctor import Status
from coordinare.init_files import fail
from coordinare.launch_preflight import (
    PROBE_TIMEOUT_SECONDS,
    _token_and_repo,
    check_github,
    check_harnesses,
)
from coordinare.presets import HARNESS_COMMANDS, HARNESS_DEFAULTS

if TYPE_CHECKING:
    from coordinare.doctor import CheckResult

API_URL = "https://api.github.com"
PROJECTS_QUERY = """
query($owner: String!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on Organization { projectsV2(first: 100, after: $after) {
      nodes { number title closed } pageInfo { hasNextPage endCursor }
    } }
    ... on User { projectsV2(first: 100, after: $after) {
      nodes { number title closed } pageInfo { hasNextPage endCursor }
    } }
  }
}
"""


async def read_command(*command: str) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        return ""
    process = await asyncio.create_subprocess_exec(
        executable, *command[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
        return stdout.decode().strip() if process.returncode == 0 else ""
    except TimeoutError:
        process.kill()
        await process.wait()
        return ""


def detected_defaults(saved_token: str = "") -> dict[str, Any]:
    token = next((os.environ[name] for name in ("COORDINARE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(name)), saved_token)
    if not token:
        token = asyncio.run(read_command("gh", "auth", "token"))
    remote = asyncio.run(read_command("git", "remote", "get-url", "origin"))
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?", remote)
    harnesses = [name for name in HARNESS_DEFAULTS if shutil.which(HARNESS_COMMANDS[name])]
    return {"github_token": token, "repository": match[1] if match else "",
            "harness": harnesses[0] if harnesses else "codex"}


def require_checks(results: list[CheckResult]) -> None:
    failures = []
    for result in results:
        if result.status is Status.WARN and result.name == "GitHub permissions":
            sys.stdout.write(result.detail + " " + (result.fix or "") + "\n")
        elif result.status in {Status.FAIL, Status.WARN}:
            failures.append(result)
    if failures:
        fail("\n".join(result.detail + " " + (result.fix or "Retry coordinare init.") for result in failures))


def probe_config(answers: dict[str, Any]) -> ProjectConfiguration:
    owner, _, repo = (answers.get("repository") or "example/setup").partition("/")
    config = ProjectConfiguration(
        preset="containers" if answers.get("run_mode") == "docker" else "solo",
        harness=answers.get("harness", "codex"), github_org=owner, github_repo=repo,
        github_project_number=answers.get("project_number") or 1,
        github_token=answers["github_token"],
        human_reviewers=["setup"],
    )
    return config.model_copy(update={"github_token": SecretStr(answers["github_token"])})


def verify_token(answers: dict[str, Any]) -> str:
    config = probe_config(answers)
    require_checks(_token_and_repo(config, answers["github_token"], probe_repo=False))
    response = httpx.get(API_URL + "/user", headers={"Authorization": "Bearer " + answers["github_token"]}, timeout=PROBE_TIMEOUT_SECONDS)
    if response.status_code != httpx.codes.OK or not response.json().get("login"):
        fail("Cannot read GitHub login. Set a valid GitHub token with repo, project and read:org scopes.")
    return str(response.json()["login"])


def verify_repository(answers: dict[str, Any]) -> None:
    require_checks(_token_and_repo(probe_config(answers), answers["github_token"], probe_repo=True))


def list_projects(owner: str, token: str) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = httpx.post(
            API_URL + "/graphql", headers={"Authorization": "Bearer " + token},
            json={"query": PROJECTS_QUERY, "variables": {"owner": owner, "after": cursor}},
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        payload = response.json()
        if response.status_code != httpx.codes.OK or payload.get("errors"):
            fail("Cannot list Projects. Grant project scope and authorize the token for organization SSO.")
        board_owner = (payload.get("data") or {}).get("repositoryOwner") or {}
        connection = board_owner.get("projectsV2")
        if not connection:
            fail("No Projects owner found. Correct the repository owner and grant Projects access.")
        projects.extend(node for node in connection["nodes"] if node and not node["closed"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return projects
        cursor = page["endCursor"]


def verify_board(answers: dict[str, Any]) -> None:
    require_checks(asyncio.run(check_github(probe_config(answers), explicit=True)))


def verify_harness(answers: dict[str, Any]) -> None:
    require_checks(asyncio.run(check_harnesses(probe_config(answers))))
