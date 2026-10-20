"""Shared bounded declaration admission; no execution or source selection policy."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery_prompts import correction_instruction
from coordinare.services.environment_proposal_patch import MAX_PATH_DEPTH, PATCH_INSTRUCTION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import ValidationError

    from coordinare.services.environment_discovery import DiscoveryLimits

_ASCII_CONTROL_BOUND = 32


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _valid_path(path: Any) -> bool:
    return (
        isinstance(path, str) and bool(path) and not path.startswith("/")
        and "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(ord(char) < _ASCII_CONTROL_BOUND for char in path)
    )


class _DuplicateJsonKeyError(ValueError):
    """Ambiguous model output must never select either duplicate value."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = "duplicate JSON key"
            raise _DuplicateJsonKeyError(message)
        result[key] = value
    return result


def _decode_reply(raw: str, name: str, limits: DiscoveryLimits) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limits.max_output_bytes:
        message = f"{name} output limit exceeded"
        raise EnvironmentDiscoveryError(message)
    try:
        reply = json.loads(raw, object_pairs_hook=_object)
    except _DuplicateJsonKeyError:
        message = f"{name} returned duplicate JSON keys"
        raise EnvironmentDiscoveryError(message, code="duplicate_json_key") from None
    except (ValueError, RecursionError):
        message = f"{name} returned malformed JSON"
        raise EnvironmentDiscoveryError(message, code="invalid_json_envelope") from None
    if not isinstance(reply, dict):
        message = f"{name} returned an invalid envelope"
        raise EnvironmentDiscoveryError(message, code="invalid_json_envelope")
    return reply


SOURCE_DISPOSITION_GUIDANCE = (
    'Each source review decision needs exactly one disposition: covered, source_correction, source_gap or duplicate, '
    'with that disposition\'s required fields. Do not supply a status field; the daemon derives it. '
    'Use criterion_ids only for covered; affected_ids, gap_kind and inspection_requests only for source_gap; '
    'duplicate_of only for duplicate. Every other decision field must be null or []. '
    'Preserve every other decision while correcting this one.')


REVISION_LIMIT_GUIDANCE = (
    'A scope revision may contain at most 16 combined add and reassess operations. Established criteria are retained '
    'without resubmission: add only genuinely new obligations and reassess only retained unknown or not_applicable '
    'criteria. Submit the most important changes now; further obligations can follow in a later revision.')
SOURCE_PATH_GUIDANCE = (
    'Each source must be one exact repository-relative file path that you inspected: no directories, ./ prefixes, '
    'trailing slashes, URLs or annotations.')


def _revision_limit(error: Mapping[str, Any], location: list[Any]) -> dict[str, Any] | None:
    """Only daemon-owned counts escape; the oversized proposal content is never echoed."""
    context = error.get('ctx') or {}
    if error['type'] == 'too_long' and 'scope_revision' in location and location[-1:] in (['add'], ['reassess']):
        return {'limit': context.get('max_length'), 'submitted': context.get('actual_length'), 'guidance': REVISION_LIMIT_GUIDANCE}
    if error['type'] == 'scope_revision_limit':
        return {'limit': context.get('limit'), 'submitted': context.get('submitted'), 'guidance': REVISION_LIMIT_GUIDANCE}
    return None


def _validation_feedback(exc: ValidationError) -> list[dict[str, Any]]:
    """Do not expose arbitrary keys, candidate values, validator context or prose."""
    allowed_fields = {"requirements", "checks", "install", "services", "activation", "id", "source", "description",
                      "depends_on", "required", "scope", "argv", "cwd", "env", "path", "timeout_seconds",
                      "schema_version", "policy_version", "empty_reason", "inputs", "manifest_digest",
                      "source_commit", "symphony_name", "sources", "verdict", "rationale", "capabilities_digest", "inventory_digest",
                      "commands", "citations", "questions", "probe", "manifest", "assessment", "findings", "decisions",
                      "finding_responses", "finding_id", "issue", "category", "affected_ids", "duplicate_of", "status",
                      "capability_unavailable", "criteria", "applicability", "usability", "runtime_needs", "scope_assessment", "criterion_checks", "runtime_endpoints", "scope_revision", "base_digest", "add", "reassess", "criterion_id", "criterion",
                      "disposition", "criterion_ids", "gap_kind", "inspection_requests", "covered", "source_correction",
                      "source_gap", "duplicate"}
    result = []
    for error in exc.errors(include_url=False, include_input=False)[:16]:
        location = [part if (isinstance(part, str) and part in allowed_fields) or type(part) is int else "*"
                    for part in error["loc"][:8]]
        kind = error["type"]
        code = kind if kind in {
            "missing", "extra_forbidden", "string_type", "list_type", "dict_type", "bool_type",
            "literal_error", "int_type", "string_pattern_mismatch", "string_too_short", "string_too_long", "too_short", "too_long",
            "requirement_check_reference", "clean_dependency_install_only", "runtime_credential_reference",
            "probe_id_maps", "probe_cited_question", "probe_required_true", "probe_citations_uninspected",
            "scope_identity_mismatch", "scope_required_check_mapping", "scope_required_service", "scope_id_collision",
            "finding_citations_invalid", "union_tag_not_found", "union_tag_invalid",
        } else "invalid_contract_value"
        scope_locations = {'scope_required_check_mapping': 'criterion_checks', 'scope_identity_mismatch': 'scope_contract_digest',
                           'scope_required_service': 'services', 'scope_id_collision': 'id'}
        if code in scope_locations:
            location = [scope_locations[code]]
        detail: dict[str, Any] = {"location": location, "code": code, **(_revision_limit(error, location) or {})}
        if code == "invalid_contract_value" and "sources" in location and "guidance" not in detail:
            detail["guidance"] = SOURCE_PATH_GUIDANCE
        if code == "finding_citations_invalid" or (code == "too_short" and location[-1:] == ["sources"]):
            detail["guidance"] = ("Supply a nonempty list of unique exact inspected file paths as source citations. "
                "Even an open finding response needs supporting source evidence. "
                "Do not use directory paths, duplicate citations or uninspected files; inspect relevant source first.")
        if code in {"union_tag_not_found", "union_tag_invalid"} or (
                code == "extra_forbidden" and "scope_assessment" in location and "decisions" in location):
            detail["guidance"] = SOURCE_DISPOSITION_GUIDANCE
        if code == "runtime_credential_reference":
            detail["guidance"] = ("Use ${NAME} for a named credential supplied by the trusted runtime. "
                "For a credential environment variable named K, its value must be exactly ${K}, not a reference to another name. "
                "Never move literal credentials into argv, activation or another field. "
                "Do not invent runtime credentials; unavailable required values must hold readiness.")
        result.append(detail)
    return result


def _repair(messages: list[dict[str, str]], raw: str, errors: list[dict[str, Any]], budget: AttemptBudget, *, can_patch: bool = False) -> None:
    if budget.scope_contract is not None:
        required_ids = [c.id for c in budget.scope_contract.definition.criteria if c.applicability == 'required']
        for error in errors:
            if error['code'] == 'scope_required_check_mapping':
                error['required_criterion_ids'] = required_ids
    if budget.expected_envelope == 'scope_assessment' and budget.scope_findings is not None:
        for error in errors:
            if error['code'] == 'review_findings_invalid' and error['location'] == ['scope_assessment', 'decisions']:
                error['required_decision_ids'] = [record.id for record in budget.scope_findings.records]
    if can_patch and budget.expected_envelope in {'manifest', 'assessment'}:
        for error in errors:
            location = error['location']
            if location and '*' not in location:
                root = budget.expected_envelope
                path = location if location[0] in {'manifest', 'finding_responses', 'assessment'} else [root, *location]
                if len(path) <= MAX_PATH_DEPTH:
                    error['correction_path'] = path
    budget.validation_codes.extend(error["code"] for error in errors)
    budget.reserve_repair("schema")
    messages.extend([{"role": "assistant", "content": raw},
                     {"role": "user", "content": _json({"validation_errors": errors,
                        "expected_envelope": budget.expected_envelope,
                        "instruction": (PATCH_INSTRUCTION if can_patch else correction_instruction(budget.expected_envelope)) +
                        "When supplied, copy correction_path exactly into the patch path; location is a diagnostic field path. "
                        "Preserve every required obligation and inspected input. "
                        "Never rename or remove existing required requirement/check/service IDs; revise their definitions in place. "
                        "Use list_files for directories and read_files only for exact file paths from listings. " +
                        ("Use required executable check IDs, not commands or install-only prerequisites. "
                         if budget.expected_envelope in {"manifest", "assessment"} else
                         "Preserve source-defined scope; do not generate commands during source review. "
                         if budget.expected_envelope in {"scope", "scope_revision", "scope_assessment"} else "") +
                        "Independent review and successful clean execution remain mandatory."})}])


def _checked_inventory(paths: Sequence[str], bounds: DiscoveryLimits) -> list[str]:
    if isinstance(paths, str) or len(paths) > bounds.max_inventory_entries:
        message = "source inventory limit exceeded or invalid inventory"
        raise EnvironmentDiscoveryError(message)
    if any(not _valid_path(path) for path in paths) or len(set(paths)) != len(paths):
        message = "source inventory contains invalid or duplicate paths"
        raise EnvironmentDiscoveryError(message)
    inventory = sorted(paths)
    if len(_json(inventory).encode()) > bounds.max_source_bytes:
        message = "source inventory byte limit exceeded"
        raise EnvironmentDiscoveryError(message)
    return inventory
