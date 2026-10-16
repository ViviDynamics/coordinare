from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml

from coordinare import docker_setup
from coordinare.config import ProjectConfiguration, expand_config_text
from coordinare.config_validation import validate_raw_config
from coordinare.init_answers import ANSWER_NAMES, collect_answers
from coordinare.init_diagnostics import init_stage, render_failure, safe_cause
from coordinare.init_files import (
    build_config,
    configure_containers,
    fail,
    read_mapping,
    secret_values,
    write_setup,
)
from coordinare.launch_preflight import run_preflight
from coordinare.service_cli import install_service
from coordinare.service_manager import ServiceError
from coordinare.services.config_secret_policy import persistent_secret_problem
from coordinare.user_config import (
    ENV_FILENAME,
    GENERATED_HEADER,
    read_environment,
    user_config_directory,
)

if TYPE_CHECKING:
    import argparse
    from typing import Any


def add_init_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("init", help="Guided setup with safe per-user configuration")
    parser.add_argument("--answers", type=Path, help="YAML mapping of setup answers")
    parser.add_argument("--config-dir", type=Path, help="Output directory (default: XDG_CONFIG_HOME/coordinare)")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; also automatic without a terminal")
    parser.add_argument("--force", action="store_true", help="Replace existing files and keep private backups")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--start", action="store_true", help="Start the daemon after successful preflight")
    startup.add_argument("--start-at-login", action="store_true", help="Install and start the user service after successful preflight")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip the throwaway Docker harness and connectivity probe on slow machines")
    for name in ANSWER_NAMES:
        parser.add_argument("--" + name.replace("_", "-"), help="Setup answer; also COORDINARE_INIT_" + name.upper())


def _validate_generated(raw: dict[str, Any], secrets: dict[str, str]) -> ProjectConfiguration:
    problem = persistent_secret_problem(raw)
    if problem:
        fail("Existing settings contain literal credentials. Replace them with ${VAR} references before rerunning init.")
    # Match normal startup precedence while keeping credentials out of the YAML on disk.
    for key, value in secrets.items():
        os.environ[key] = value
    expanded = yaml.safe_load(expand_config_text(GENERATED_HEADER + "\n" + yaml.safe_dump(raw)))
    result = validate_raw_config(expanded)
    if not result.passed:
        fail("Configuration is invalid. Check setup answers and existing advanced settings: " + ", ".join(error.field_path for error in result.errors))
    return ProjectConfiguration(**expanded)


def _setup(args: argparse.Namespace) -> int:
    directory = (args.config_dir or user_config_directory()).expanduser().resolve()
    config_path = directory / "config.yaml"
    env_path = directory / ENV_FILENAME
    interactive = not args.non_interactive and sys.stdin.isatty()
    existing = any(path.exists() or path.is_symlink() for path in (config_path, env_path))
    if not interactive and not args.force and existing:
        fail("Setup files already exist. Use --force to keep backups and replace them.")
    raw = read_mapping(config_path) if config_path.exists() else {}
    if "symphonies" in raw or raw.get("preset") not in {None, "solo", "containers"}:
        fail("This configuration uses advanced orchestration. Edit it with coordinare config; init supports starter presets.")
    previous = read_environment(env_path)
    answers = collect_answers(args, raw, previous)
    if interactive and existing and input("Replace existing setup files and keep backups [no]: ").strip().lower() not in {"y", "yes"}:
        fail("Setup cancelled; existing files were kept.")
    secrets = secret_values(answers, previous)
    init_stage.set("model_endpoint")
    generated = build_config(answers, raw)
    if answers["run_mode"] == "docker":
        image = docker_setup.prepare_image(answers["image_variant"], answers.get("image_tag"), directory)
        init_stage.set("container_setup")
        configure_containers(generated, image, secrets, config_directory=directory)
    init_stage.set("model_endpoint")
    config = _validate_generated(generated, secrets)
    if answers["run_mode"] == "docker":
        init_stage.set("container_smoke")
        if args.skip_smoke:
            sys.stdout.write("Docker smoke skipped; harness readiness and container connectivity are unverified.\n")
        else:
            for endpoint in config.performer_endpoints:
                if endpoint.mode == "ephemeral":
                    docker_setup.smoke_container(endpoint, connectivity=docker_setup.smoke_environment(config))
        sys.stdout.write("After upgrading, re-pin with coordinare init --force --run-mode docker (or add --image-tag VERSION for a development install).\n")
    init_stage.set("write_config")
    args.config = write_setup(directory, generated, secrets, force=args.force or (interactive and existing))
    sys.stdout.write(f"Saved configuration: {args.config}\nSecrets: {env_path} (0600)\n")
    init_stage.set("preflight")
    report = asyncio.run(run_preflight(config, raw=generated, explicit=True, probe_containers=answers["run_mode"] != "docker"))
    sys.stdout.write(report.render())
    sys.stdout.write("Start: coordinare --config " + shlex.quote(str(args.config)) + "\n")
    sys.stdout.write(f"Dashboard: http://{config.dashboard_host}:{config.dashboard_port}\n")
    if not report.ok:
        return 1
    init_stage.set("service")
    return _offer_start(args, interactive=interactive)


def _offer_start(args: argparse.Namespace, *, interactive: bool) -> int:
    """After a clean preflight: start at login as a user service, or start in the foreground."""
    if interactive and not args.start and not args.start_at_login:
        args.start_at_login = input("Start at login [no]: ").strip().lower() in {"y", "yes"}
    if args.start_at_login:
        try:
            install_service(args.config)
        except (ServiceError, OSError, ValueError) as exc:
            fail("Setup saved, but service installation failed. Check your login session and run coordinare service install --config " + shlex.quote(str(args.config)), cause=safe_cause(exc))
        sys.stdout.write("User service installed and started. Manage it with coordinare service.\n")
        return 0
    if interactive and not args.start:
        args.start = input("Start Coordinare now [no]: ").strip().lower() in {"y", "yes"}
    return 0


def run_init(args: argparse.Namespace) -> int:
    stage_token = init_stage.set("answers")
    environment = os.environ.copy()
    for name in ("github_token", "api_key"):
        if getattr(args, name) is not None:
            flag = "--" + name.replace("_", "-")
            variable = "COORDINARE_INIT_" + name.upper()
            sys.stderr.write(f"Warning: {flag} can expose credentials in shell history and process listings. Use {variable} or a private --answers file instead.\n")
    try:
        return _setup(args)
    except (ValueError, OSError, yaml.YAMLError, httpx.HTTPError, EOFError) as exc:
        # Exceptions from YAML, HTTP and filesystem libraries can contain credentials.
        # Only controlled SetupError messages and structured metadata are safe to show.
        sys.stderr.write(render_failure(exc))
        return 1
    finally:
        init_stage.reset(stage_token)
        if not args.start:
            os.environ.clear()
            os.environ.update(environment)
