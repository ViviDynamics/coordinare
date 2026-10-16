from __future__ import annotations

import getpass
import os
import sys
import warnings
from typing import TYPE_CHECKING, Any

from coordinare import init_probes
from coordinare.init_diagnostics import init_stage
from coordinare.init_files import fail, read_mapping
from coordinare.presets import HARNESS_DEFAULTS

if TYPE_CHECKING:
    import argparse

ANSWER_NAMES = (
    "github_token", "repository", "project_number", "harness", "model", "endpoint",
    "api_key", "max_concurrent_cards", "human_reviewers", "run_mode", "image_variant", "image_tag",
)
REPOSITORY_PARTS = 2


def supplied_answers(args: argparse.Namespace) -> dict[str, Any]:
    supplied = read_mapping(args.answers) if args.answers else {}
    unknown = set(supplied) - set(ANSWER_NAMES)
    if unknown:
        fail("Unknown setup answers: " + ", ".join(sorted(unknown)))
    for name in ANSWER_NAMES:
        value = getattr(args, name)
        environment = os.environ.get("COORDINARE_INIT_" + name.upper())
        if environment is not None:
            supplied[name] = environment
        if value is not None:
            supplied[name] = value
    _validate_answer_types(supplied)
    return supplied


def _validate_answer_types(supplied: dict[str, Any]) -> None:
    for name, value in supplied.items():
        if name == "human_reviewers" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            continue
        if name in {"project_number", "max_concurrent_cards"} and type(value) is int:
            continue
        if not isinstance(value, str):
            fail("Invalid answer type for " + name + ". Use text, positive integers or a list of reviewer logins.")


def existing_defaults(raw: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    defaults = init_probes.detected_defaults(secrets.get("COORDINARE_GITHUB_TOKEN", ""))
    if raw.get("github_org") and raw.get("github_repo"):
        defaults["repository"] = raw["github_org"] + "/" + raw["github_repo"]
    defaults.update({
        "project_number": raw.get("github_project_number"),
        "harness": raw.get("harness", defaults["harness"]),
        "max_concurrent_cards": raw.get("max_concurrent_cards", 1),
        "human_reviewers": raw.get("human_reviewers"),
        "run_mode": "docker" if raw.get("preset") == "containers" else "local",
        "endpoint": "",
        "image_variant": "base" if "-base:" in str(raw.get("performer_image", "")) else "full",
    })
    for entry in raw.get("model_endpoints", []):
        if entry.get("name") == "starter":
            defaults["model"] = entry["model"]
    for entry in raw.get("endpoints", []):
        if entry.get("name") == "starter":
            defaults["endpoint"] = entry.get("base_url", "")
    return defaults


class Questions:
    def __init__(self, supplied: dict[str, Any], defaults: dict[str, Any], *, interactive: bool) -> None:
        self.supplied = supplied
        self.answers = {**defaults, **supplied}
        self.interactive = interactive

    def ask(self, name: str, label: str, *, secret: bool = False) -> Any:
        default = self.answers.get(name)
        if name not in self.supplied and self.interactive:
            # A saved secret is never shown; pressing Enter keeps it, and typing replaces it.
            display = "saved credential, Enter keeps it" if secret and default else default
            if isinstance(display, list):
                display = ",".join(display)
            prompt = f"{label} [{display or 'none'}]: "
            value = _read_secret(prompt) if secret else input(prompt)
            self.answers[name] = value or default
        return self.answers.get(name)


def _read_secret(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        fail("This terminal cannot hide secret input. Supply credentials through environment variables or a private --answers file.")


def _positive_integer(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError):
        fail(f"{name} must be a positive integer.")
    if isinstance(value, bool) or number < 1 or str(number) != str(value):
        fail(f"{name} must be a positive integer.")
    return number


def _choose_project(questions: Questions) -> None:
    init_stage.set("board")
    if not questions.answers.get("project_number") and questions.interactive:
        owner = questions.answers["repository"].split("/")[0]
        boards = init_probes.list_projects(owner, questions.answers["github_token"])
        if not boards:
            fail("No open Projects found. Create a Projects V2 board with lifecycle Status columns.")
        for board in boards:
            sys.stdout.write(f"  {board['number']}: {board['title']}\n")
        questions.answers["project_number"] = boards[0]["number"]
    value = questions.ask("project_number", "Project board number")
    questions.answers["project_number"] = _positive_integer(value, "project_number")
    init_probes.verify_board(questions.answers)


def _choose_harness(questions: Questions, secrets: dict[str, str]) -> None:
    init_stage.set("model_endpoint")
    harness = questions.ask("harness", "Harness (installed CLIs preferred)")
    if harness not in HARNESS_DEFAULTS:
        fail("Choose a supported starter harness: codex, claude_code or opencode.")
    _kind, credential, model = HARNESS_DEFAULTS[harness]
    questions.answers.setdefault("model", model)
    questions.ask("model", "Model")
    questions.ask("endpoint", "Endpoint URL (none uses the native provider)")
    key = os.environ.get(credential) or secrets.get(credential, "")
    questions.answers.setdefault("api_key", key)
    label = (f"{credential} (required for native providers in Docker)"
             if questions.answers.get("run_mode") == "docker" else f"{credential} (none uses the harness login)")
    questions.ask("api_key", label, secret=True)
    if harness == "opencode" and not questions.answers.get("api_key") and not questions.answers.get("endpoint"):
        fail("api_key is required for the opencode native provider. Set OPENAI_API_KEY or --api-key.")
    if questions.answers.get("run_mode") == "docker":
        if not questions.answers.get("api_key") and not questions.answers.get("endpoint"):
            fail("Docker mode requires a model API key for a native provider. Set " + credential + " or supply api_key; host harness login files are not mounted.")
    else:
        init_stage.set("harness")
        init_probes.verify_harness(questions.answers)


def collect_answers(args: argparse.Namespace, raw: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    supplied = supplied_answers(args)
    questions = Questions(supplied, existing_defaults(raw, secrets), interactive=not args.non_interactive and sys.stdin.isatty())
    if not questions.interactive:
        missing = [name for name in ("github_token", "repository", "project_number") if not questions.answers.get(name)]
        if missing:
            fail("Missing setup answers: " + ", ".join(missing) + ". Supply flags, --answers or COORDINARE_INIT_* variables.")
    questions.ask("github_token", "GitHub token (repo, project, read:org)", secret=True)
    if not questions.answers.get("github_token"):
        fail("github_token is required. Set GH_TOKEN or log in with gh auth login.")
    init_stage.set("github")
    login = init_probes.verify_token(questions.answers)
    init_stage.set("answers")
    repository = questions.ask("repository", "Repository owner/name")
    if not isinstance(repository, str) or len(repository.split("/")) != REPOSITORY_PARTS or not all(repository.split("/")):
        fail("repository must be owner/name. Supply --repository or run inside its git checkout.")
    init_stage.set("github")
    init_probes.verify_repository(questions.answers)
    _choose_project(questions)
    init_stage.set("answers")
    mode = questions.ask("run_mode", "Run mode: local processes or docker")
    if mode not in {"local", "docker"}:
        fail("run_mode must be local or docker.")
    if mode == "docker":
        sys.stdout.write("Images: base has core tools (allow 4 GiB); full adds Chromium and security/test tools (allow 10 GiB). Actual sizes vary by release and architecture.\n")
        questions.ask("image_variant", "Image variant: base or full")
    _choose_harness(questions, secrets)
    init_stage.set("answers")
    sys.stdout.write("More concurrent cards increase model cost and the number of PRs needing review.\n")
    questions.ask("max_concurrent_cards", "Concurrent cards")
    questions.answers["max_concurrent_cards"] = _positive_integer(questions.answers["max_concurrent_cards"], "max_concurrent_cards")
    questions.answers["human_reviewers"] = questions.answers.get("human_reviewers") or [login]
    reviewers = questions.ask("human_reviewers", "Human reviewer logins, comma separated")
    questions.answers["human_reviewers"] = [part.strip() for part in reviewers.split(",") if part.strip()] if isinstance(reviewers, str) else reviewers
    if not questions.answers["human_reviewers"]:
        fail("human_reviewers must include at least one GitHub login.")
    return questions.answers
