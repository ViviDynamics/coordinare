"""Deterministic preservation of established environment obligations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import EnvironmentManifest, VerificationReport

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_scope import preserve_scope

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import Command


def _required_execution_commands(manifest: EnvironmentManifest) -> list[Command]:
    commands = [command for command in manifest.checks if command.required]
    for service in manifest.services:
        if service.required:
            commands.extend(
                command
                for command in (service.init, service.start, service.health, service.stop)
                if command is not None
            )
    return commands


def _executed_manifest(entry: Any, source_commit: str) -> EnvironmentManifest | None:
    manifest = EnvironmentManifest.model_validate(entry["manifest"])
    data = dict(entry["report"])
    data.pop("passed", None)
    report = VerificationReport.model_validate(data)
    digest = manifest.digest()
    by_id = {result.id: result for result in report.results}
    if (
        manifest.source_commit != source_commit
        or entry["manifest_digest"] != digest
        or report.manifest_digest != digest
        or len(by_id) != len(report.results)
        or any(
            result.manifest_digest != digest or result.duration_seconds < 0
            for result in report.results
        )
        or not any(result.required and result.status == "fail" for result in report.results)
    ):
        message = "execution history identity or results differ"
        raise ValueError(message)
    if not any(
        result.exit_code is not None or result.duration_seconds > 0 for result in report.results
    ):
        return None  # Trusted preflight stopped every project command before execution.
    required = [
        command
        for command in _required_execution_commands(manifest)
        if not report.scope or not command.scope or report.scope in command.scope
    ]
    if any(command.id not in by_id or not by_id[command.id].required for command in required):
        message = "execution history omits required commands"
        raise ValueError(message)
    return manifest


_MAX_REJECTED_EXECUTIONS = 3
_MAX_EXECUTION_HISTORY_BYTES = 4 * 1024 * 1024


def _bounded_history(history: object) -> list[Any]:
    if (
        not isinstance(history, list)
        or len(history) > _MAX_REJECTED_EXECUTIONS
        or len(json.dumps(history, allow_nan=False).encode()) > _MAX_EXECUTION_HISTORY_BYTES
    ):
        message = "execution history exceeds its bound"
        raise ValueError(message)
    return history


def executed_manifests(history: object, *, source_commit: str) -> list[EnvironmentManifest]:
    """Derive pins from bounded existing evidence, including after daemon restart."""
    try:
        return [
            manifest
            for entry in _bounded_history(history)
            if (manifest := _executed_manifest(entry, source_commit)) is not None
        ]
    except (KeyError, TypeError, ValueError):
        message = "execution history is malformed or unbound"
        raise EnvironmentDiscoveryError(message, code="execution_history_invalid") from None


def preserve_execution_commands(
    manifest: EnvironmentManifest,
    previous: EnvironmentManifest,
) -> None:
    """Repair the environment around established checks, never rewrite their result."""
    current = {command.id: command for command in _required_execution_commands(manifest)}
    for old in _required_execution_commands(previous):
        new = current.get(old.id)
        if (
            new is None
            or new.model_dump(exclude={"timeout_seconds"})
            != old.model_dump(exclude={"timeout_seconds"})
            or new.timeout_seconds < old.timeout_seconds
        ):
            message = f"required executable command {old.id} changed after project execution"
            raise EnvironmentDiscoveryError(message, code="obligation_removed")


def preserve_execution_obligations(manifest: EnvironmentManifest, history: object) -> None:
    for previous in executed_manifests(history, source_commit=manifest.source_commit):
        preserve_execution_commands(manifest, previous)


def preserve_obligations(manifest: EnvironmentManifest, previous: EnvironmentManifest) -> None:
    """A fresh judgment can add coverage, never waive a previously required obligation."""
    if previous.scope_contract is not None:
        if manifest.scope_contract is None:
            message = 'required source scope removed during repair'
            raise EnvironmentDiscoveryError(message, code='obligation_removed')
        preserve_scope(previous.scope_contract.definition, manifest.scope_contract.definition)
    if any(not set(ids) <= set(manifest.criterion_checks.get(identifier, []))
           for identifier, ids in previous.criterion_checks.items()):
        message = 'required source criterion coverage removed during repair'
        raise EnvironmentDiscoveryError(message, code='obligation_removed')
    for group in ("requirements", "install", "checks", "services"):
        current = {item.id: item for item in getattr(manifest, group) if item.required}
        for old in getattr(previous, group):
            if not old.required:
                continue
            new = current.get(old.id)
            if new is None:
                message = "required obligation removed during repair"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group == "requirements" and not set(old.checks) <= set(new.checks):
                message = "required obligation coverage removed"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group in {"checks", "install"} and old.scope != new.scope:
                message = "required obligation scope changed"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group == "services":
                for phase in ("init", "start", "health", "stop"):
                    before, after = getattr(old, phase), getattr(new, phase)
                    if before is not None and (
                        after is None
                        or before.id != after.id
                        or before.scope != after.scope
                        or (before.required and not after.required)
                    ):
                        message = "required service obligation changed"
                        raise EnvironmentDiscoveryError(message, code="obligation_removed")
